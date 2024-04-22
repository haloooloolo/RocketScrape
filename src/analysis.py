import logging
import heapq
import re
import json
import copy

import discord
import rocketscrape

import matplotlib.pyplot as plt
import numpy as np

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Any, Generic, TypeVar, Union, Callable, Awaitable
from datetime import datetime, timedelta
from tabulate import tabulate

from client import Client
from messages import MessageStream, Message, UserIDType, ChannelIDType

T = TypeVar('T')


@dataclass
class CustomArgument:
    name: str
    type: type[Any]
    help: Optional[str] = None


@dataclass
class CustomFlag:
    name: str
    help: Optional[str] = None


@dataclass
class CustomOption:
    name: str
    type: type[Any]
    default: Any
    help: Optional[str] = None


ArgType = Union[CustomArgument, CustomOption, CustomFlag]


@dataclass
class Result(Generic[T]):
    start: Optional[datetime]
    end: Optional[datetime]
    data: T
    __display: Callable[['Result[T]', Client, int], Awaitable[None]]

    async def display(self, client: Client, max_results: int):
        await self.__display(self, client, max_results)


class MessageAnalysis(ABC, Generic[T]):
    def __init__(self, stream: MessageStream, args):
        self.log_interval = timedelta(seconds=args.log_interval)
        self.stream = stream

    async def run(self, start: Optional[datetime], end: Optional[datetime]) -> Result[T]:
        assert (start is None) or (end is None) or (end > start)
        last_ts = datetime.now()
        self._prepare()

        async for message in self.stream.get_history(start, end, self._require_reactions):
            ts = datetime.now()
            if (ts - last_ts) >= self.log_interval:
                logging.info(f'Message stream reached {message.created}')
                last_ts = ts

            self._on_message(message)

        return Result(start, end, self._finalize(), self._display_result)

    @property
    @abstractmethod
    def _require_reactions(self) -> bool:
        pass

    @abstractmethod
    def _prepare(self) -> None:
        pass

    @abstractmethod
    def _on_message(self, message: Message) -> None:
        pass

    @abstractmethod
    def _finalize(self) -> T:
        pass

    @staticmethod
    def _get_date_range_str(start: Optional[datetime], end: Optional[datetime]) -> str:
        if start and end:
            range_str = f'from {start} to {end}'
        elif start:
            range_str = f'since {start}'
        elif end:
            range_str = f'up to {end}'
        else:
            range_str = '(all time)'

        return range_str

    @abstractmethod
    async def _display_result(self, result: Result[T], client: Client, max_results: int) -> None:
        pass

    @staticmethod
    @abstractmethod
    def subcommand() -> str:
        pass

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return ()


class CountBasedMessageAnalysis(MessageAnalysis[dict[UserIDType, int]]):
    def _prepare(self) -> None:
        self.count: dict[UserIDType, int] = {}

    @abstractmethod
    def _on_message(self, message: Message) -> None:
        pass

    def _finalize(self) -> dict[UserIDType, int]:
        return self.count

    @abstractmethod
    def _title(self) -> str:
        pass

    async def _display_result(self, result: Result[dict[UserIDType, int]], client: Client, max_results: int) -> None:
        range_str = self._get_date_range_str(result.start, result.end)
        top_users = heapq.nlargest(max_results, result.data.items(), key=lambda a: a[1])

        print(f'{self._title()} {range_str}')
        for i, (user_id, count) in enumerate(top_users):
            print(f'{i + 1}. {await client.try_fetch_username(user_id)}: {count}')


S = TypeVar('S', bound=MessageAnalysis)


class HistoryBasedMessageAnalysis(MessageAnalysis[tuple[list[datetime], list[T]]], Generic[S, T]):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self._base_analysis = self._base_analysis_class()(stream, args)
        self.interval = timedelta(days=1)

    @classmethod
    @abstractmethod
    def _base_analysis_class(cls) -> type[S]:
        pass

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return cls._base_analysis_class().custom_args()

    @property
    def _require_reactions(self) -> bool:
        return self._base_analysis._require_reactions

    def _prepare(self) -> None:
        self._base_analysis._prepare()
        self.next_date: Optional[datetime] = None
        self.last_ts: Optional[datetime] = None
        self.x: list[datetime] = []
        self.y: list[T] = []

    def __add_data_point(self, dt: datetime) -> None:
        self.x.append(dt)
        self.y.append(self._get_data())

    @abstractmethod
    def _get_data(self) -> T:
        pass

    def _on_message(self, message: Message) -> None:
        self._base_analysis._on_message(message)
        self.last_ts = message.created

        if self.next_date is None:
            self.next_date = message.created
        elif message.created < self.next_date:
            return

        self.__add_data_point(message.created)
        self.next_date += self.interval

    def _finalize(self) -> tuple[list[datetime], list[T]]:
        self._base_analysis._finalize()
        if self.last_ts:
            self.__add_data_point(self.last_ts)

        return self.x, self.y


class TopContributorAnalysis(MessageAnalysis):
    def __init__(self, stream, args):
        super().__init__(stream, args)
        self.base_session_time = args.base_session_time
        self.session_timeout = args.session_timeout

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return MessageAnalysis.custom_args() + (
            CustomOption('base-session-time', int, 5),
            CustomOption('session-timeout', int, 15)
        )

    @property
    def _require_reactions(self) -> bool:
        return False

    @staticmethod
    def __to_minutes(td: timedelta) -> float:
        return td / timedelta(minutes=1)

    def _prepare(self) -> None:
        self.open_sessions: dict[UserIDType, tuple[datetime, datetime]] = {}
        self.total_time: dict[UserIDType, float] = {}

    def _get_session_time(self, start: datetime, end: datetime) -> float:
        return self.__to_minutes(end - start) + self.base_session_time

    def _close_session(self, author_id: UserIDType, start: datetime, end: datetime) -> None:
        session_time = self._get_session_time(start, end)
        self.total_time[author_id] = self.total_time.get(author_id, 0.0) + session_time

    def _on_message(self, message: Message) -> None:
        timestamp = message.created
        author_id = message.author_id

        session_start, session_end = self.open_sessions.get(author_id, (timestamp, timestamp))
        if self.__to_minutes(timestamp - session_end) < self.session_timeout:
            # extend session to current timestamp
            self.open_sessions[author_id] = (session_start, timestamp)
        else:
            self._close_session(author_id, session_start, session_end)
            del self.open_sessions[author_id]

    def _finalize(self) -> dict[UserIDType, float]:
        # end remaining sessions
        for author_id, (session_start, session_end) in self.open_sessions.items():
            self._close_session(author_id, session_start, session_end)

        return self.total_time

    async def _display_result(self, result: Result[dict[UserIDType, float]], client: Client, max_results: int) -> None:
        range_str = self._get_date_range_str(result.start, result.end)
        top_contributors = heapq.nlargest(max_results, result.data.items(), key=lambda a: a[1])

        print(f'Top {self.stream} contributors {range_str}')
        for i, (user_id, time) in enumerate(top_contributors):
            time_mins = round(time)
            hours, minutes = time_mins // 60, time_mins % 60
            print(f'{i + 1}. {await client.try_fetch_username(user_id)}: {hours}h {minutes}m')

    @staticmethod
    def subcommand() -> str:
        return 'contributors'


class ContributorHistoryAnalysis(HistoryBasedMessageAnalysis[TopContributorAnalysis, dict[UserIDType, float]]):
    @classmethod
    def _base_analysis_class(cls) -> type[TopContributorAnalysis]:
        return TopContributorAnalysis

    def _get_data(self) -> dict[UserIDType, float]:
        return copy.copy(self._base_analysis.total_time)

    async def _display_result(self, result: Result[tuple[list[datetime], list[dict[UserIDType, float]]]],
                              client: Client, max_results: int) -> None:
        x, y = result.data

        times_by_user: dict[UserIDType, list[float]] = {}
        for i, snapshot in enumerate(y):
            for author, time_min in snapshot.items():
                if author not in times_by_user:
                    times_by_user[author] = [0.0] * i
                times_by_user[author].append(time_min)

        for user_id, data in sorted(times_by_user.items(), key=lambda a: a[1][-1], reverse=True)[:max_results]:
            plt.plot(np.array(x), np.array(data), label=(await client.try_fetch_username(user_id))
                     .encode('ascii', 'ignore').decode('ascii'))

        plt.ylabel('time (mins)')
        plt.legend()
        plt.title(f'Top {self.stream} contributors over time'
                  .encode('ascii', 'ignore').decode('ascii'))
        plt.show()

    @staticmethod
    def subcommand() -> str:
        return 'contributor-history'


class MessageCountAnalysis(CountBasedMessageAnalysis):
    def _on_message(self, message: Message) -> None:
        self.count[message.author_id] = self.count.get(message.author_id, 0) + 1

    @property
    def _require_reactions(self) -> bool:
        return False

    def _title(self) -> str:
        return f'Top {self.stream} contributors by message count'

    @staticmethod
    def subcommand() -> str:
        return 'message-count'


class SelfKekAnalysis(CountBasedMessageAnalysis):
    @property
    def _require_reactions(self) -> bool:
        return True

    def _on_message(self, message: Message) -> None:
        for emoji_name, users in message.reactions.items():
            if ('kek' in emoji_name) and (message.author_id in users):
                self.count[message.author_id] = self.count.get(message.author_id, 0) + 1

    def _title(self) -> str:
        return f'Top {self.stream} self kek offenders'

    @staticmethod
    def subcommand() -> str:
        return 'self-kek'


class MissingPersonAnalysis(TopContributorAnalysis):
    def __init__(self, stream, args):
        super().__init__(stream, args)
        self.inactivity_threshold = timedelta(days=args.inactivity_threshold)

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return TopContributorAnalysis.custom_args() + (
            CustomOption('inactivity_threshold', int, 90,
                         'number of days without activity required to be considered inactive'),
        )

    @property
    def _require_reactions(self) -> bool:
        return False

    def _prepare(self) -> None:
        super()._prepare()
        self.last_seen: dict[UserIDType, datetime] = {}
        self.last_ts: Optional[datetime] = None

    def _on_message(self, message: Message) -> None:
        super()._on_message(message)
        self.last_ts = message.created
        self.last_seen[message.author_id] = message.created

    def _finalize(self) -> dict[UserIDType, float]:
        total_time = super()._finalize()
        for author_id, ts in self.last_seen.items():
            assert self.last_ts is not None
            if (self.last_ts - ts) < self.inactivity_threshold:
                del total_time[author_id]

        return total_time

    async def _display_result(self, result: Result[dict[UserIDType, float]], client: Client, args) -> None:
        top_contributors = heapq.nlargest(args.max_results, result.data.items(), key=lambda a: a[1])

        print(f'Top {self.stream} contributors with no recent activity ')
        for i, (author_id, time) in enumerate(top_contributors):
            time_mins = round(time)
            hours, minutes = time_mins // 60, time_mins % 60
            print(f'{i + 1}. {await client.try_fetch_username(author_id)}: {hours}h {minutes}m')

    @staticmethod
    def subcommand() -> str:
        return 'missing-persons'


class ReactionsGivenAnalysis(CountBasedMessageAnalysis):
    @property
    def _require_reactions(self) -> bool:
        return True

    def _on_message(self, message: Message) -> None:
        for emoji_name, users in message.reactions.items():
            for user_id in users:
                self.count[user_id] = self.count.get(user_id, 0) + 1

    def _title(self) -> str:
        return f'{self.stream} members with most reactions given'

    @staticmethod
    def subcommand() -> str:
        return 'total-reactions-given'


class ReactionsReceivedAnalysis(CountBasedMessageAnalysis):
    @property
    def _require_reactions(self) -> bool:
        return True

    def _on_message(self, message: Message) -> None:
        for emoji_name, users in message.reactions.items():
            self.count[message.author_id] = self.count.get(message.author_id, 0) + len(users)

    def _title(self) -> str:
        return f'{self.stream} members with most reactions received'

    @staticmethod
    def subcommand() -> str:
        return 'total-reactions-received'


class ThankYouCountAnalysis(CountBasedMessageAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.__pattern = re.compile('\b(ty)|(thank(?:s| (?:yo)?u)?)|(thx)\b')

    @property
    def _require_reactions(self) -> bool:
        return False

    def _on_message(self, message: Message) -> None:
        content = message.content.lower()
        if not self.__pattern.search(content):
            return

        mentions: set[UserIDType] = message.mentions
        if message.reference:
            if replied_to := self.stream.get_message(message.reference):
                mentions.add(replied_to.author_id)

        for user_id in mentions:
            self.count[user_id] = self.count.get(user_id, 0) + 1

    def _title(self) -> str:
        return f'{self.stream} members thanked most often'

    @staticmethod
    def subcommand() -> str:
        return 'thank-count'


class ReactionReceivedAnalysis(CountBasedMessageAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.emoji = args.react

    @property
    def _require_reactions(self) -> bool:
        return True

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return CountBasedMessageAnalysis.custom_args() + (
            CustomArgument('react', str, 'emoji to count received reactions for'),
        )

    def _on_message(self, message: Message) -> None:
        if self.emoji in message.reactions:
            num_reactions = len(message.reactions[self.emoji])
            self.count[message.author_id] = self.count.get(message.author_id, 0) + num_reactions

    def _title(self) -> str:
        return f'{self.stream} members by {self.emoji} received'

    @staticmethod
    def subcommand() -> str:
        return 'reaction-received-count'


class ReactionGivenAnalysis(CountBasedMessageAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.emoji = args.react

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return CountBasedMessageAnalysis.custom_args() + (
            CustomArgument('react', str, 'emoji to count given reactions for'),
        )

    @property
    def _require_reactions(self) -> bool:
        return True

    def _on_message(self, message: Message) -> None:
        for user in message.reactions.get(self.emoji, []):
            self.count[user] = self.count.get(user, 0) + 1

    def _title(self) -> str:
        return f'{self.stream} members by {self.emoji} given'

    @staticmethod
    def subcommand() -> str:
        return 'reaction-given-count'


class ActivityTimeAnalyis(MessageAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.user: UserIDType = args.user
        self.num_buckets: int = args.num_buckets
        assert (24 * 60 % self.num_buckets) == 0, \
            'bucket count doesn\'t cleanly divide into minutes'
        self.key_format: str = args.key_format

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return MessageAnalysis.custom_args() + (
            CustomArgument('user', UserIDType),
            CustomOption('num-buckets', int, 24),
            CustomOption('key-format', str, '',
                         'split messages based on time, supports $y, $q, $m, $d (e.g. Q$q $y)'),
        )

    @property
    def _require_reactions(self) -> bool:
        return False

    def _prepare(self) -> None:
        self.buckets: dict[str, list[int]] = {}

    def _get_key(self, timestamp: datetime) -> str:
        return self.key_format \
            .replace('$y', str(timestamp.year)) \
            .replace('$q', str(int((timestamp.month + 2) / 3))) \
            .replace('$m', str(timestamp.month)) \
            .replace('$d', str(timestamp.day))

    def _on_message(self, message: Message) -> None:
        if message.author_id != self.user:
            return

        timestamp = message.created.astimezone()
        key = self._get_key(timestamp)
        if key not in self.buckets:
            self.buckets[key] = [0] * self.num_buckets

        bucket = int((60 * timestamp.hour + timestamp.minute) / (24 * 60 / self.num_buckets))
        self.buckets[key][bucket] += 1

    def _finalize(self) -> dict[str, list[int]]:
        return self.buckets

    async def _display_result(self, result: Result[dict[str, list[int]]], client: Client, max_results: int) -> None:
        x = np.arange(self.num_buckets)

        labels = []
        bucket_width = int(24 * 60 / self.num_buckets)
        for i in x:
            start = i * bucket_width
            end = start + bucket_width - 1
            start_fmt = f'{(start // 60):02}:{(start % 60):02}'
            end_fmt = f'{(end // 60):02}:{(end % 60):02}'
            labels.append(f'{start_fmt} - {end_fmt}')

        full_width = 0.75
        bar_width = full_width / len(result.data)
        offset = (bar_width - full_width) / 2
        for key, buckets in result.data.items():
            plt.bar(x + offset, buckets, bar_width, label=key)
            offset += bar_width

        plt.xticks(x, labels, rotation=90)
        plt.xlim(-1, self.num_buckets)
        plt.ylabel('message count')

        if self.key_format:
            plt.legend(loc='upper left')

        username = await client.try_fetch_username(self.user)
        title = f'{username} message activity in {self.stream} by local time'
        plt.title(title.encode('ascii', 'ignore').decode('ascii'))

        plt.subplots_adjust(bottom=0.25)
        plt.show()

    @staticmethod
    def subcommand() -> str:
        return 'message-time-histogram'


class WordCountAnalysis(MessageAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.word: str = args.word
        self.ignore_case = args.ignore_case

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return MessageAnalysis.custom_args() + (
            CustomArgument('word', str),
            CustomFlag('ignore-case', 'make word match case-insensitive'),
        )

    @property
    def _require_reactions(self) -> bool:
        return False

    def _prepare(self) -> None:
        self.count: int = 0

    def _finalize(self) -> int:
        return self.count

    def _on_message(self, message: Message) -> None:
        word, content = self.word, message.content
        if self.ignore_case:
            word, content = word.lower(), content.lower()

        if word in content:
            self.count += 1

    async def _display_result(self, result: Result[int], client: Client, max_results: int) -> None:
        range_str = self._get_date_range_str(result.start, result.end)
        print(f'{result.data} occurrences of "{self.word}" in {self.stream} {range_str}')

    @staticmethod
    def subcommand() -> str:
        return 'word-count'


class SupportBountyAnalysis(MessageAnalysis):
    class __SupportBountyHelper(TopContributorAnalysis):
        def _prepare(self) -> None:
            super()._prepare()
            self.time_by_month: dict[tuple[int, int], dict[UserIDType, float]] = {}

        def _close_session(self, author_id: UserIDType, start: datetime, end: datetime) -> None:
            session_time = self._get_session_time(start, end)
            month = (end.month, end.year)
            if month not in self.time_by_month:
                self.time_by_month[month] = {}
            self.time_by_month[month][author_id] = self.time_by_month[month].get(author_id, 0) + session_time

        @staticmethod
        def subcommand() -> str:
            return ''

    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.min_monthly_activity = args.min_monthly_activity
        self.__helper = self.__SupportBountyHelper(stream, args)

    @property
    def _require_reactions(self) -> bool:
        return False

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return cls.__SupportBountyHelper.custom_args() + (
            CustomOption('min-monthly-activity', int, 60,
                         'minimum required activity per month in minutes'),
        )

    def _prepare(self) -> None:
        self.__helper._prepare()

    def _on_message(self, message: Message) -> None:
        self.__helper._on_message(message)

    def _finalize(self) -> dict[tuple[int, int], dict[UserIDType, float]]:
        self.__helper._finalize()
        return self.__helper.time_by_month

    async def _display_result(self, result: Result[dict[tuple[int, int], dict[UserIDType, float]]],
                              client: Client, max_results: int) -> None:
        exclusion_list: set[UserIDType] = set()

        team_role_id = 405169632195117078
        if core_team_role := await client.try_fetch_role(team_role_id, rocketscrape.Server.rocketpool):
            team_members = {member.id for member in await core_team_role.fetch_members()}
            exclusion_list.update(team_members)
        else:
            logging.warning(f'Could not fetch Rocket Pool team members (role id {team_role_id})')

        def user_eligible(_user) -> bool:
            if _user is None:
                return True
            return not (_user.bot or (_user.id in exclusion_list))

        time_by_user: dict[UserIDType, dict[tuple[int, int], float]] = {}

        for month, users in result.data.items():
            for user_id, time in users.items():
                if user_id not in time_by_user:
                    time_by_user[user_id] = {}
                time_by_user[user_id][month] = time

        months: list[tuple[int, int]] = list(result.data.keys())
        headers = ['user', 'total'] + [f'{m:02}/{y}' for (m, y) in months]
        contributors: list[tuple[str, list[float]]] = []

        for user_id, monthly_data in time_by_user.items():
            total_time = sum(monthly_data.values())
            monthly_times = [monthly_data.get(m, 0.0) for m in months]
            if total_time < self.min_monthly_activity * len(months):
                continue

            user = await client.try_fetch_user(user_id)
            if not user_eligible(user):
                continue

            username = await client.try_fetch_username(user or user_id)
            if (total_time < 300 * len(months)) and any((t < self.min_monthly_activity for t in monthly_times)):
                username = '* ' + username

            row = (username, [total_time] + monthly_times)
            contributors.append(row)

        def fmt_h_m(_time: float) -> str:
            time_mins = round(_time)
            hours, minutes = time_mins // 60, time_mins % 60
            return f'{hours}h {minutes:02}m'

        table = []
        for row in heapq.nlargest(max_results, contributors, key=lambda a: a[1]):
            name, times = row
            table.append([name] + [fmt_h_m(t) for t in times])

        print(tabulate(table, headers=headers, colalign=('left',), stralign='right'))

    @staticmethod
    def subcommand() -> str:
        return 'support-bounty'


class ThreadListAnalysis(MessageAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.user_id: UserIDType = args.user

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return MessageAnalysis.custom_args() + (
            CustomArgument('user', UserIDType),
        )

    @property
    def _require_reactions(self) -> bool:
        return False

    def _prepare(self) -> None:
        self.channel_ids: set[ChannelIDType] = set()

    def _on_message(self, message: Message) -> None:
        if message.author_id == self.user_id:
            self.channel_ids.add(message.channel_id)

    def _finalize(self) -> set[ChannelIDType]:
        return self.channel_ids

    async def _display_result(self, result: Result[set[ChannelIDType]], client: Client, max_results: int) -> None:
        server_id = rocketscrape.Server.rocketpool.value
        for channel_id in result.data:
            channel = await client.try_fetch_channel(channel_id)
            if isinstance(channel, discord.Thread):
                print(f'https://discord.com/channels/{server_id}/{channel_id}')

    @staticmethod
    def subcommand() -> str:
        return 'thread-list'


class TimeToThresholdAnalysis(MessageAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.__analysis = TopContributorAnalysis(stream, args)
        self.threshold = args.time_threshold

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return TopContributorAnalysis.custom_args() + (
            CustomOption('time-threshold', int, 50_000),
        )

    @property
    def _require_reactions(self) -> bool:
        return False

    def _prepare(self) -> None:
        self.__analysis._prepare()
        self.y: dict[UserIDType, list[float]] = {}
        self.next_dates: dict[UserIDType, datetime] = {}

    def _on_message(self, message: Message) -> None:
        self.__analysis._on_message(message)
        author_id = message.author_id

        if author_id not in self.next_dates:
            self.next_dates[author_id] = message.created
        elif message.created < self.next_dates[author_id]:
            return

        time_min = self.__analysis.total_time.get(author_id, 0.0)

        if author_id not in self.y:
            self.y[author_id] = [0.0]
        if self.y[author_id][-1] < self.threshold:
            self.y[author_id].append(min(time_min, self.threshold))

        self.next_dates[author_id] += timedelta(days=1)

    def _finalize(self) -> dict[UserIDType, list[float]]:
        self.__analysis._finalize()
        return self.y

    async def _display_result(self, result: Result[dict[UserIDType, list[int]]],
                              client: Client, max_results: int) -> None:
        y = {u: d for (u, d) in result.data.items() if d[-1] >= self.threshold}

        for user_id, data in sorted(y.items(), key=lambda a: len(a[1]))[:max_results]:
            username = await client.try_fetch_username(user_id)
            plt.plot(np.arange(len(data)), np.array(data), label=username
                     .encode('ascii', 'ignore').decode('ascii'))

        plt.xlabel('days')
        plt.ylabel('time (mins)')
        plt.legend()
        plt.title(f'Fastest {self.stream} contributors to reach {self.threshold:,} minutes'
                  .encode('ascii', 'ignore').decode('ascii'))
        plt.show()

    @staticmethod
    def subcommand() -> str:
        return 'days-to-threshold'


class UniqueUserHistoryAnalysis(
        HistoryBasedMessageAnalysis['UniqueUserHistoryAnalysis.__UniqueUserCountAnalysis', int]):
    class __UniqueUserCountAnalysis(MessageAnalysis):
        @property
        def _require_reactions(self) -> bool:
            return False

        def _prepare(self) -> None:
            self.users: set[UserIDType] = set()

        def _on_message(self, message: Message) -> None:
            self.users.add(message.author_id)

        def _finalize(self) -> int:
            return len(self.users)

        async def _display_result(self, result: Result[tuple[int, int]], client: Client, max_results: int) -> None:
            start, end = result.start, result.end
            print(f'Unique user count for {self.stream} {self._get_date_range_str(start, end)}')
            print(result.data)

        @staticmethod
        def subcommand() -> str:
            return ''

    @classmethod
    def _base_analysis_class(cls) -> type[__UniqueUserCountAnalysis]:
        return cls.__UniqueUserCountAnalysis

    def _get_data(self) -> int:
        return len(self._base_analysis.users)

    async def _display_result(self, result: Result[tuple[list[datetime], list[int]]],
                              client: Client, max_results: int) -> None:
        x, y = result.data
        plt.plot(np.array(x), np.array(y))
        title = f'Number of unique {self.stream} participants over time'

        plt.title(title.encode('ascii', 'ignore').decode('ascii'))
        plt.show()

    @staticmethod
    def subcommand() -> str:
        return 'unique-user-history'


class WickPenaltyHistoryAnalysis(
        HistoryBasedMessageAnalysis['WickPenaltyHistoryAnalysis.__WickPenaltyCountAnalysis', tuple[int, int]]):
    class __WickPenaltyCountAnalysis(MessageAnalysis):
        @property
        def _require_reactions(self) -> bool:
            return False

        def _prepare(self) -> None:
            self.bans = 0
            self.timeouts = 0

        def _on_message(self, message: Message) -> None:
            if message.author_id != 536991182035746816:
                return

            if not message.embeds:
                return

            description = message.embeds[0].get('description', '')
            if 'banned' in description:
                self.bans += 1
            elif any((kw in description for kw in ('timed out', 'silenced'))):
                self.timeouts += 1

        def _finalize(self) -> tuple[int, int]:
            return self.bans, self.timeouts

        async def _display_result(self, result: Result[tuple[int, int]], client: Client, max_results: int) -> None:
            start, end = result.start, result.end
            bans, timeouts = result.data

            print(f'Wick penalty count for {self.stream} {self._get_date_range_str(start, end)}')
            print(f'Total bans: {bans}')
            print(f'Total timeouts: {timeouts}')

        @staticmethod
        def subcommand() -> str:
            return ''

    @classmethod
    def _base_analysis_class(cls) -> type[__WickPenaltyCountAnalysis]:
        return cls.__WickPenaltyCountAnalysis

    def _get_data(self) -> tuple[int, int]:
        return self._base_analysis.bans, self._base_analysis.timeouts

    async def _display_result(self, result: Result[tuple[list[datetime], list[tuple[int, int]]]],
                              client: Client, max_results: int) -> None:
        x, y = result.data
        x_arr, y_arr = np.array(x), np.array(y)

        plt.plot(x_arr, y_arr[:, 0], label='Ban', color='firebrick')
        plt.plot(x_arr, y_arr[:, 1], label='Timeout', color='orange')
        title = f'Cumulative Wick Penalty Count ({self.stream})'
        plt.title(title.encode('ascii', 'ignore').decode('ascii'))
        plt.legend()
        plt.show()

    @staticmethod
    def subcommand() -> str:
        return 'wick-penalties'


class JSONExport(MessageAnalysis):
    JSONFieldType = Optional[Union[int, str, list]]
    JSONMessageType = dict[str, JSONFieldType]

    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.file_path = args.file_path or f'{self.stream}.json'
        self.include_reactions = args.include_reactions
        self.include_usernames = args.include_usernames

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return TopContributorAnalysis.custom_args() + (
            CustomOption('file-path', str, None),
            CustomFlag('include-reactions'),
            CustomFlag('include-usernames'),
        )

    @property
    def _require_reactions(self) -> bool:
        return self.include_reactions

    def _prepare(self) -> None:
        self.data: dict[str, list[JSONExport.JSONMessageType]] = {'messages': []}

    def _on_message(self, message: Message) -> None:
        msg_data = {k: str(v) if isinstance(v, datetime) else v for (k, v) in message.__dict__.items()}
        self.data['messages'].append(msg_data)

    def _finalize(self) -> dict[str, list[JSONMessageType]]:
        return self.data

    async def _display_result(self, result: Result[dict[str, list[JSONMessageType]]], client: Client,
                              max_results: int) -> None:
        async def fetch_all_usernames() -> None:
            logging.info('Fetching usernames')
            usernames: dict[UserIDType, Optional[str]] = {}

            for msg in result.data['messages']:
                user_id = msg['author_id']
                assert isinstance(UserIDType, int)
                username: Optional[str] = None

                if user_id in usernames:
                    username = usernames[user_id]
                else:
                    if user := await client.try_fetch_user(user_id):
                        username = user.display_name
                    usernames[user_id] = username

                msg['author_username'] = username

        if self.include_usernames:
            await fetch_all_usernames()

        logging.info(f'Saving file to {self.file_path}')
        with open(self.file_path, 'w') as f:
            json.dump(result.data, f, indent=4)

    @staticmethod
    def subcommand() -> str:
        return 'json-dump'
