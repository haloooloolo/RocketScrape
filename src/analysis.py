import logging
import heapq
import re
from dataclasses import dataclass

from abc import ABC, abstractmethod
from typing import Optional, Any, Generic, TypeVar, Union, Callable, Awaitable
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import numpy as np

import rocketscrape
from client import Client
from messages import MessageStream, Message

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


class CountBasedMessageAnalysis(MessageAnalysis):
    def _prepare(self) -> None:
        self.count: dict[int, int] = {}

    @abstractmethod
    def _on_message(self, message: Message) -> None:
        pass

    def _finalize(self) -> dict[int, int]:
        return self.count

    @abstractmethod
    def _title(self) -> str:
        pass

    async def _display_result(self, result: Result[dict[int, int]], client: Client, max_results: int) -> None:
        range_str = self._get_date_range_str(result.start, result.end)
        top_users = heapq.nlargest(max_results, result.data.items(), key=lambda a: a[1])

        print()
        print(f'{self._title()} {range_str}')
        for i, (user_id, count) in enumerate(top_users):
            print(f'{i + 1}. {await client.try_fetch_username(user_id)}: {count}')


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
        self.open_sessions: dict[int, tuple[datetime, datetime]] = {}
        self.total_time: dict[int, float] = {}

    def _get_session_time(self, start: datetime, end: datetime) -> float:
        return self.__to_minutes(end - start) + self.base_session_time

    def _close_session(self, author_id: int, start: datetime, end: datetime) -> None:
        session_time = self._get_session_time(start, end)
        self.total_time[author_id] = self.total_time.get(author_id, 0) + session_time

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

    def _finalize(self) -> dict[int, float]:
        # end remaining sessions
        for author_id, (session_start, session_end) in self.open_sessions.items():
            self._close_session(author_id, session_start, session_end)

        return self.total_time

    async def _display_result(self, result: Result[dict[int, float]], client: Client, max_results: int) -> None:
        range_str = self._get_date_range_str(result.start, result.end)
        top_contributors = heapq.nlargest(max_results, result.data.items(), key=lambda a: a[1])

        print()
        print(f'Top {self.stream} contributors {range_str}')
        for i, (user_id, time) in enumerate(top_contributors):
            time_mins = round(time)
            hours, minutes = time_mins // 60, time_mins % 60
            print(f'{i + 1}. {await client.try_fetch_username(user_id)}: {hours}h {minutes}m')

    @staticmethod
    def subcommand() -> str:
        return 'contributors'


class ContributorHistoryAnalysis(MessageAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.__contributor_analysis = TopContributorAnalysis(stream, args)
        self.interval = timedelta(days=args.snapshot_interval)

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return TopContributorAnalysis.custom_args() + (
            CustomOption('snapshot-interval', int, 7, 'time between data snapshots in days'),
        )

    @property
    def _require_reactions(self) -> bool:
        return False

    def _prepare(self) -> None:
        self.__contributor_analysis._prepare()
        self.x: list[datetime] = []
        self.y: dict[int, list[float]] = {}
        self.next_date: Optional[datetime] = None
        self.last_ts: Optional[datetime] = None

    def __add_snapshot(self, dt: datetime) -> None:
        for author, time_min in self.__contributor_analysis.total_time.items():
            if author not in self.y:
                self.y[author] = [0.0] * len(self.x)
            self.y[author].append(time_min)

        self.x.append(dt)

    def _on_message(self, message: Message) -> None:
        self.__contributor_analysis._on_message(message)
        self.last_ts = message.created

        if self.next_date is None:
            self.next_date = message.created
        elif message.created < self.next_date:
            return

        self.__add_snapshot(self.next_date)
        self.next_date += self.interval

    def _finalize(self) -> tuple[list[datetime], dict[int, list[float]]]:
        self.__contributor_analysis._finalize()
        if self.last_ts:
            self.__add_snapshot(self.last_ts)
        return self.x, self.y

    async def _display_result(self, result: Result[tuple[list[datetime], dict[int, list[float]]]],
                              client: Client, max_results: int) -> None:
        x, y = result.data
        for user_id, data in sorted(y.items(), key=lambda a: a[1][-1], reverse=True)[:max_results]:
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
        self.last_seen: dict[int, datetime] = {}
        self.last_ts: Optional[datetime] = None

    def _on_message(self, message: Message) -> None:
        super()._on_message(message)
        self.last_ts = message.created
        self.last_seen[message.author_id] = message.created

    def _finalize(self) -> dict[int, float]:
        total_time = super()._finalize()
        for author_id, ts in self.last_seen.items():
            assert self.last_ts is not None
            if (self.last_ts - ts) < self.inactivity_threshold:
                del total_time[author_id]

        return total_time

    async def present(self, result: dict[int, float], client: Client, args) -> None:
        top_contributors = heapq.nlargest(args.max_results, result.items(), key=lambda a: a[1])

        print()
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
        self.__pattern = re.compile('((^| |\n)(ty)( |$|\n|.|!))|(thank(s| you)?)|(thx)')

    @property
    def _require_reactions(self) -> bool:
        return False

    def _on_message(self, message: Message) -> None:
        content = message.content.lower()
        if not self.__pattern.search(content):
            return

        mentions = message.mentions
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
        self.user: int = args.user
        self.num_buckets: int = args.num_buckets
        assert (24 * 60 % self.num_buckets) == 0, \
            'bucket count doesn\'t cleanly divide into minutes'
        self.key_format: str = args.key_format

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return MessageAnalysis.custom_args() + (
            CustomArgument('user', int),
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
        print('')
        print(f'{result.data} occurrences of "{self.word}" in {self.stream} {range_str}')

    @staticmethod
    def subcommand() -> str:
        return 'word-count'


class SupportBountyAnalysis(TopContributorAnalysis):
    def __init__(self, stream: MessageStream, args):
        super().__init__(stream, args)
        self.min_monthly_activity = args.min_monthly_activity

    @classmethod
    def custom_args(cls) -> tuple[ArgType, ...]:
        return TopContributorAnalysis.custom_args() + (
            CustomOption('min-monthly-activity', int, 60),
        )

    def _prepare(self) -> None:
        self.open_sessions: dict[int, tuple[datetime, datetime]] = {}
        self.time_by_month: dict[tuple[int, int], dict[int, float]] = {}

    def _close_session(self, author_id: int, start: datetime, end: datetime) -> None:
        session_time = self._get_session_time(start, end)
        month = (end.month, end.year)
        if month not in self.time_by_month:
            self.time_by_month[month] = {}
        self.time_by_month[month][author_id] = self.time_by_month[month].get(author_id, 0) + session_time

    def _finalize(self) -> dict[int, float]:
        for author_id, (session_start, session_end) in self.open_sessions.items():
            self._close_session(author_id, session_start, session_end)

        total_time: dict[int, float] = {}
        participation_count: dict[int, int] = {}

        for monthly_data in self.time_by_month.values():
            for author_id, time in monthly_data.items():
                if time >= self.min_monthly_activity:
                    participation_count[author_id] = participation_count.get(author_id, 0) + 1
                    total_time[author_id] = total_time.get(author_id, 0) + time

        for author_id in list(total_time.keys()):
            if participation_count[author_id] < len(self.time_by_month):
                del total_time[author_id]

        return total_time

    async def _display_result(self, result: Result[dict[int, float]], client: Client, max_results: int) -> None:
        team_role_id = 405169632195117078
        core_team_role = await client.try_fetch_role(team_role_id, rocketscrape.Server.rocketpool)

        if core_team_role is not None:
            team_members = {member.id for member in await core_team_role.fetch_members()}
        else:
            logging.warning(f'Could not fetch Rocket Pool team members (role id {team_role_id})')
            team_members = set()

        contributors: list[tuple[str, float]] = []

        def user_eligible(_user) -> bool:
            if _user is None:
                return True
            return not (_user.bot or (_user.id in team_members))

        for user_id, time in result.data.items():
            user = await client.try_fetch_user(user_id)
            if user_eligible(user):
                username = await client.try_fetch_username(user or user_id)
                contributors.append((username, time))

        top_contributors = heapq.nlargest(max_results, contributors, key=lambda a: a[1])
        range_str = self._get_date_range_str(result.start, result.end)

        def fmt_h_m(_time: float) -> str:
            time_mins = round(_time)
            hours, minutes = time_mins // 60, time_mins % 60
            return f'{hours}h {minutes}m'

        print()
        print(f'Suggested {self.stream} bounty recipients {range_str}')
        for i, (username, time) in enumerate(top_contributors):
            print(f'{i + 1}. {username}: {fmt_h_m(time)}')

    @staticmethod
    def subcommand() -> str:
        return 'support-bounty'
