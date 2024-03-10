import os
import copy
import shutil
import pickle
import discord

from typing import Optional
from datetime import datetime, timezone
from dataclasses import dataclass

CACHE_DIR = 'cache'


@dataclass
class Message:
    def __init__(self, message: discord.Message) -> None:
        self.time: datetime = message.created_at
        self.author: str = message.author.name
        self.content: str = message.content

    def __repr__(self) -> str:
        return f'Message{{{self.author} @ {self.time}: "{self.content}"}}'


@dataclass
class _Segment:
    start: datetime
    end: datetime
    messages: list[Message]

    def __repr__(self):
        return f'{{{self.start}, {self.end}, [{len(self.messages)}]}}'

    def merge(self, others: list['_Segment']):
        self.start = min(self.start, others[0].start)
        self.end = max(self.end, others[-1].end)

        other_messages = sum([s.messages for s in others], [])[::-1]
        self_messages = self.messages[::-1]
        self.messages = []

        while self_messages or other_messages:
            if (not other_messages) or (self_messages and (self_messages[-1].time <= other_messages[-1].time)):
                self.messages.append(self_messages.pop())
            else:
                self.messages.append(other_messages.pop())


class MessageCache:
    def __init__(self, channel: discord.TextChannel) -> None:
        self.channel = channel
        self.uncommitted_messages = []
        try:
            self.segments = self.__load()
        except (FileNotFoundError, EOFError):
            self.segments = []

    def __load(self) -> list[_Segment]:
        path = os.path.join(CACHE_DIR, f'{self.channel.id}.pkl')
        with open(path, 'rb') as file:
            return pickle.load(file)

    def __commit(self, start: Optional[datetime], end: Optional[datetime]) -> None:
        if not end and not self.uncommitted_messages:
            return

        start = start or datetime.fromtimestamp(0).replace(tzinfo=timezone.utc)
        end = end or self.uncommitted_messages[-1].time
        low, high, successor = None, None, None

        for segment_nr, segment in enumerate(self.segments):
            if end < segment.start:
                successor = segment_nr
            elif start <= segment.end:  # segments overlap
                low = segment_nr if (low is None) else segment_nr
                high = segment_nr

        new_segment = _Segment(start, end, copy.copy(self.uncommitted_messages))
        self.uncommitted_messages.clear()

        if (low is not None) and (high is not None):
            new_segment.merge(self.segments[low:high + 1])
            self.segments = self.segments[:low] + [new_segment] + self.segments[high + 1:]
        elif successor is not None:
            self.segments.insert(successor, new_segment)
        else:
            self.segments.append(new_segment)

        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, f'{self.channel.id}.pkl')
        backup_path = os.path.join(CACHE_DIR, f'{self.channel.id}_backup.pkl')

        if os.path.exists(path):
            shutil.move(path, backup_path)

        with open(path, 'wb') as file:
            pickle.dump(self.segments, file)

        if os.path.exists(backup_path):
            os.remove(backup_path)

    async def get_history(self, start: Optional[datetime], end: Optional[datetime]):
        last_timestamp = start

        def process_message(_message, from_cache=False):
            nonlocal last_timestamp
            last_timestamp = _message.time

            if from_cache:
                return

            self.uncommitted_messages.append(_message)
            if len(self.uncommitted_messages) >= 10_000:
                print(f'committing {len(self.uncommitted_messages)} new messages to disk...')
                self.__commit(start, self.uncommitted_messages[-1].time)

        for segment in copy.copy(self.segments):
            # segment ahead of requested interval, skip
            if start and start > segment.end:
                continue

            # fill gap between last retrieved message and start of this interval
            async for m in self.channel.history(limit=None, after=last_timestamp, before=segment.start, oldest_first=True):
                message = Message(m)
                if end and message.time > end:
                    self.__commit(start, end)
                    return

                process_message(message)
                yield message

            for message in segment.messages:
                if end and message.time > end:
                    self.__commit(start, end)
                    return 

                if (start is None) or (message.time >= start):
                    process_message(message, from_cache=True)
                    yield message

        # fill gap between last segment end of requested interval
        async for m in self.channel.history(limit=None, after=last_timestamp, before=end, oldest_first=True):
            message = Message(m)
            process_message(message)
            yield message

        self.__commit(start, end)
