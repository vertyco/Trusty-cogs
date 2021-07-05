import re
import logging
import pytz

from dateutil import parser
from dateutil.tz import gettz
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple, cast, Dict

from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list, pagify, humanize_timedelta
from redbot.core.i18n import Translator, cog_i18n

import discord
from discord.utils import snowflake_time
from discord.ext.commands.converter import Converter
from discord.ext.commands.errors import BadArgument

log = logging.getLogger("red.trusty-cogs.EventPoster")

_ = Translator("EventPoster", __file__)

IMAGE_LINKS = re.compile(r"(http[s]?:\/\/[^\"\']*\.(?:png|jpg|jpeg|gif|png))", flags=re.I)

# the following regex is slightly modified from Red
# it's changed to be slightly more strict on matching with finditer
# this is to prevent "empty" matches when parsing the full reason
# This is also designed more to allow time interval at the beginning or the end of the mute
# to account for those times when you think of adding time *after* already typing out the reason
# https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/develop/redbot/core/commands/converter.py#L55
TIME_RE_STRING = r"|".join(
    [
        r"((?P<weeks>\d+?)\s?(weeks?|w))",
        r"((?P<days>\d+?)\s?(days?|d))",
        r"((?P<hours>\d+?)\s?(hours?|hrs|hr?))",
        r"((?P<minutes>\d+?)\s?(minutes?|mins?|m(?!o)))",  # prevent matching "months"
        r"((?P<seconds>\d+?)\s?(seconds?|secs?|s))",
    ]
)
TIME_RE = re.compile(TIME_RE_STRING, re.I)


class TimeZones:
    def __init__(self):
        self.zones = {}
        self._last_updated = 0

    def get_zones(self):
        delta = datetime.now() - datetime.fromtimestamp(self._last_updated)
        if not self.zones or delta > timedelta(days=1):
            # only generate a new list of timezones daily
            # This should save some processing time while still
            # giving flexibility based on timezones available
            self.gen_tzinfos()
            return self.zones
        else:
            return self.zones

    def gen_tzinfos(self):
        self._last_updated = datetime.now().timestamp()
        self.zones = {}
        # reset so we don't end up with two timezones present at once daily
        for zone in pytz.common_timezones:
            try:
                tzdate = pytz.timezone(zone).localize(datetime.utcnow(), is_dst=None)
            except pytz.NonExistentTimeError:
                # This catches times that don't exist due to Daylight savings time
                pass
            else:
                tzinfo = gettz(zone)
                self.zones[tzdate.tzname()] = tzinfo
                # store the timezone info into a dict to be returned
                # for the parser to understand common timezone short names


TIMEZONES = TimeZones()


class JoinEventButton(discord.ui.Button):
    def __init__(self, custom_id: str):
        super().__init__(
            style=discord.ButtonStyle.green, label=_("Join Event"), custom_id=custom_id
        )

    async def callback(self, interaction: discord.Interaction):
        """Join this event"""
        if interaction.user.id in self.view.members:
            await interaction.response.send_message(
                _("You have already registered for this event."), ephemeral=True
            )
            return
        if self.view.max_slots and len(self.view.members) >= self.view.max_slots:
            await interaction.response.send_message(
                _("This event is at the maximum number of members."), ephemeral=True
            )
            return
        if interaction.user.id in self.view.maybe:
            self.view.maybe.remove(interaction.user.id)
        self.view.members.append(interaction.user.id)
        self.view.check_join_enabled()
        await self.view.update_event()


class LeaveEventButton(discord.ui.Button):
    def __init__(self, custom_id: str):
        super().__init__(
            style=discord.ButtonStyle.red, label=_("Leave Event"), custom_id=custom_id
        )

    async def end_event(self, interaction: discord.Interaction):
        try:
            await self.view.end_event()
        except Exception:
            pass
        await interaction.response.edit_message(content=_("Your event has now ended."), view=None)

    async def keep_event(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=_("I will not end this event."), view=None)

    async def callback(self, interaction: discord.Interaction):
        """Leave this event"""
        if interaction.user.id == self.view.hoster:
            new_view = discord.ui.View()
            approve_button = discord.ui.Button(style=discord.ButtonStyle.green, label=_("Yes"))
            approve_button.callback = self.end_event
            deny_button = discord.ui.Button(style=discord.ButtonStyle.red, label=_("No"))
            deny_button.callback = self.keep_event
            new_view.add_item(approve_button)
            new_view.add_item(deny_button)
            await interaction.response.send_message(
                content=_("Are you sure you want to end your event?"),
                ephemeral=True,
                view=new_view,
            )
            return
        if interaction.user.id not in self.view.members:
            await interaction.response.send_message(
                _("You are not registered for this event."), ephemeral=True
            )
            return
        if interaction.user.id in self.view.members:
            self.view.members.remove(interaction.user.id)
        if interaction.user.id in self.view.maybe:
            self.view.maybe.remove(interaction.user.id)
        self.view.check_join_enabled()

        await self.view.update_event()


class PlayerClassSelect(discord.ui.Select):
    def __init__(
        self, custom_id: str, options: Dict[str, str], placeholder: Optional[str]
    ):
        super().__init__(
            custom_id=custom_id,
            min_values=1,
            max_values=1,
            placeholder=placeholder,
        )
        for option, emoji in options.items():
            self.add_option(label=option, emoji=emoji)

    async def callback(self, interaction: discord.Interaction):
        if self.view.max_slots and len(self.view.members) >= self.view.max_slots:
            await interaction.response.send_message(
                _("This event is at the maximum number of members."), ephemeral=True
            )
            return
        if interaction.user.id in self.view.maybe:
            self.view.maybe.remove(interaction.user.id)
        if interaction.user.id not in self.view.members:
            self.view.members.append(interaction.user.id)
        await self.view.cog.config.member(interaction.user).player_class.set(self.values[0])
        self.view.check_join_enabled()
        await self.view.update_event()


class MaybeJoinEventButton(discord.ui.Button):
    def __init__(self, custom_id: str):
        super().__init__(
            style=discord.ButtonStyle.grey, label=_("Maybe Join Event"), custom_id=custom_id
        )

    async def callback(self, interaction: discord.Interaction):
        """Maybe Join this event"""
        if interaction.user.id == self.view.hoster:
            await interaction.response.send_message(
                _("You are hosting this event, you cannot join the maybe queue!"), ephemeral=True
            )
            return
        if interaction.user.id in self.view.members:
            self.view.members.remove(interaction.user.id)
            self.view.check_join_enabled()
        if interaction.user.id in self.view.maybe:
            self.view.maybe.remove(interaction.user.id)
        else:
            self.view.maybe.append(interaction.user.id)

        await self.view.update_event()


class Event(discord.ui.View):
    bot: Red
    hoster: int
    members: List[int]
    event: str
    max_slots: Optional[int]
    approver: Optional[int]
    message: Optional[int]
    channel: Optional[int]
    guild: int
    maybe: List[int]
    start: Optional[datetime]

    def __init__(self, **kwargs):
        self.bot = kwargs.get("bot")
        self.hoster = kwargs.get("hoster")
        self.members = kwargs.get("members")
        self.event = kwargs.get("event")
        self.max_slots = kwargs.get("max_slots")
        self.approver = kwargs.get("approver")
        self.message = kwargs.get("message")
        self.channel = kwargs.get("channel")
        self.guild = kwargs.get("guild")
        self.maybe = kwargs.get("maybe", [])
        self.start = kwargs.get("start", None)
        self.select_options = kwargs.get("select_options", {})
        self.cog = kwargs.get("cog")
        super().__init__(timeout=None)
        self.join_button = JoinEventButton(custom_id=f"join-{self.hoster}")
        self.leave_button = LeaveEventButton(custom_id=f"leave-{self.hoster}")
        self.maybe_button = MaybeJoinEventButton(custom_id=f"maybejoin-{self.hoster}")
        self.add_item(self.join_button)
        self.add_item(self.maybe_button)
        self.add_item(self.leave_button)
        self.select_view = None
        if self.select_options:
            self.select_view = PlayerClassSelect(
                custom_id=f"playerclass-{self.hoster}",
                options=self.select_options,
                placeholder=_("Pick a class to join this event"),
            )
            self.add_item(self.select_view)

    def __repr__(self):
        return "<Event description={0.event} hoster={0.hoster} start={0.start}>".format(self)

    def check_join_enabled(self):
        if self.max_slots and len(self.members) >= self.max_slots:
            self.join_button.disabled = True
            log.debug(f"Setting Join Button to {self.join_button.disabled}")
        if self.max_slots and len(self.members) < self.max_slots:
            self.join_button.disabled = False
            log.debug(f"Setting Join Button to {self.join_button.disabled}")

    async def interaction_check(self, interaction: discord.Interaction):
        """
        The interaction pre-check incase I ever need it

        Right now there are no restrictions on joining events
        """
        return True

    async def start_time(self) -> Optional[datetime]:
        date = None
        if self.start is None:
            # assume it's a timedelta first
            # if it's not a timedelta we can try searching for a date
            time_data = {}
            for time in TIME_RE.finditer(self.event):
                for k, v in time.groupdict().items():
                    if v:
                        time_data[k] = int(v)
            if time_data:
                date = datetime.now(timezone.utc) + timedelta(**time_data)
            else:
                try:
                    date, tokens = parser.parse(
                        self.event, fuzzy_with_tokens=True, tzinfos=TIMEZONES.get_zones()
                    )
                    if date and "tomorrow" in self.event.lower():
                        date += timedelta(days=1)
                    date.replace(tzinfo=timezone.utc)
                except Exception:
                    log.debug("Error parsing datetime.")
            if date:
                log.debug("setting start date")
                self.start = date
                return date
            else:
                return None
        else:
            return self.start

    def should_remove(self, seconds: int) -> bool:
        """
        Returns True if we should end the event
        Returns False if the event should stay open
        """
        now = datetime.now(timezone.utc).timestamp()
        if self.message is None:
            # If we don't even have a message linked to this event delete it
            # although in practice this should never happen
            return True
        if self.start:
            future = (self.start + timedelta(seconds=seconds)).timestamp()
            log.debug(f"{humanize_timedelta(seconds = future-now)}")
            return now > future
        else:
            future = (
                snowflake_time(self.message).replace(tzinfo=timezone.utc)
                + timedelta(seconds=seconds)
            ).timestamp()
            log.debug(f"{humanize_timedelta(seconds = future-now)}")
            return now > future

    def remaining(self, seconds: int) -> str:
        """
        Returns the time remaining on an event
        """
        now = datetime.now(timezone.utc).timestamp()
        if self.message is None:
            # If we don't even have a message linked to this event delete it
            # although in practice this should never happen
            return _("0 seconds")
        if self.start:
            future = (self.start + timedelta(seconds=seconds)).timestamp()
            diff = future - now
            log.debug(f"Set time {future=} {now=} {diff=}")
            return humanize_timedelta(seconds=future - now)
        else:
            future = (
                snowflake_time(self.message).replace(tzinfo=timezone.utc)
                + timedelta(seconds=seconds)
            ).timestamp()
            diff = future - now
            log.debug(f"Message Time {future=} {now=} {diff=}")
            return humanize_timedelta(seconds=future - now)

    async def update_event(self):
        ctx = await self.get_ctx(self.bot)
        em = await self.make_event_embed(ctx)
        await self.edit(ctx, embed=em)
        config = self.bot.get_cog("EventPoster").config
        async with config.guild_from_id(self.guild).events() as cur_events:
            cur_events[str(self.hoster)] = self.to_json()
        self.bot.get_cog("EventPoster").event_cache[self.guild][self.message] = self

    async def end_event(self):
        config = self.bot.get_cog("EventPoster").config
        async with config.guild_from_id(self.guild).events() as events:
            # event = Event.from_json(self.bot, events[str(user.id)])
            ctx = await self.get_ctx(self.bot)
            if ctx:
                await self.edit(ctx, content=_("This event has ended."))
            del events[str(self.hoster)]
            del self.bot.get_cog("EventPoster").event_cache[self.guild][self.message]

    async def get_ctx(self, bot: Red) -> Optional[commands.Context]:
        """
        Returns the context object for the events message

        This can't be used to invoke another command but
        it is useful to get a basis for an events final posted message.
        """
        guild = bot.get_guild(self.guild)
        if not guild:
            return None
        chan = guild.get_channel(self.channel)
        if not chan:
            return None
        try:
            msg = await chan.fetch_message(self.message)
        except (discord.errors.NotFound, discord.errors.Forbidden):
            return None
        return await bot.get_context(msg)

    async def edit(self, context: commands.Context, **kwargs) -> None:
        ctx = await self.get_ctx(context.bot)
        if not ctx:
            return
        await ctx.message.edit(**kwargs, view=self)

    def mention(self, include_maybe: bool):
        members = self.members
        if include_maybe:
            members += self.maybe
        return humanize_list([f"<@!{m}>" for m in members])

    async def make_event_embed(self, ctx: Optional[commands.Context] = None) -> discord.Embed:
        if ctx is None:
            ctx = await self.get_ctx(self.bot)
        hoster = ctx.guild.get_member(self.hoster)
        em = discord.Embed()
        em.set_author(
            name=_("{hoster} is hosting").format(hoster=hoster), icon_url=hoster.avatar.url
        )
        try:
            prefixes = await ctx.bot.get_valid_prefixes(ctx.guild)
            prefix = prefixes[0]
        except AttributeError:
            prefixes = await ctx.bot.get_prefix(ctx.message)
            prefix = prefixes[0]
        max_slots_msg = ""
        if self.max_slots:
            slots = self.max_slots - len(self.members)
            if slots < 0:
                slots = 0
            max_slots_msg = _("**{slots} slots available.**").format(slots=slots)

        em.description = _(
            "**{description}**\n\nTo join this event type "
            "`{prefix}join {hoster}` or react to this message with "
            "\N{WHITE HEAVY CHECK MARK}\n\n{max_slots_msg} "
        ).format(
            description=self.event[:1024],
            prefix=prefix,
            hoster=hoster,
            max_slots_msg=max_slots_msg,
        )
        player_list = ""
        config = Config.get_conf(None, identifier=144014746356678656, cog_name="EventPoster")
        for i, member in enumerate(self.members):
            player_class = ""
            has_player_class = await config.member_from_ids(ctx.guild.id, member).player_class()
            mem = ctx.guild.get_member(member)
            if has_player_class:
                player_class = f" - {has_player_class}"
            player_list += _("**Slot {slot_num}**\n{member}{player_class}\n").format(
                slot_num=i + 1, member=mem.mention, player_class=player_class
            )
        for page in pagify(player_list, page_length=1024):
            em.add_field(name=_("Attendees"), value=page)
        if self.maybe and len(em.fields) < 25:
            maybe = [f"<@!{m}>" for m in self.maybe]
            em.add_field(name=_("Maybe"), value=humanize_list(maybe))
        if self.approver:
            approver = ctx.guild.get_member(self.approver)
            em.set_footer(
                text=_("Approved by {approver}").format(approver=approver),
                icon_url=approver.avatar.url,
            )
        start = await self.start_time()
        if start is not None:
            em.timestamp = start

        thumbnails = await config.guild(ctx.guild).custom_links()
        for name, link in thumbnails.items():
            if name.lower() in self.event.lower():
                em.set_thumbnail(url=link)
        return em

    @classmethod
    def from_json(cls, bot: Red, data: dict):
        members = data.get("members", [])
        new_members = []
        for m in members:
            if isinstance(m, tuple) or isinstance(m, list):
                log.debug(f"Converting to new members list in {data.get('channel')}")
                new_members.append(m[0])
            else:
                new_members.append(m)
        start = data.get("start")
        if start:
            start = datetime.fromtimestamp(start, tz=timezone.utc)
        guild = data.get("guild")
        if not guild:
            chan = bot.get_channel(data.get("channel"))
            guild = chan.guild.id
        return cls(
            bot=bot,
            hoster=data.get("hoster"),
            members=new_members,
            event=data.get("event"),
            max_slots=data.get("max_slots"),
            approver=data.get("approver"),
            message=data.get("message"),
            guild=guild,
            channel=data.get("channel"),
            maybe=data.get("maybe"),
            start=start,
            select_options=data.get("select_options"),
        )

    def to_json(self):
        return {
            "hoster": self.hoster,
            "members": self.members,
            "event": self.event,
            "max_slots": self.max_slots,
            "approver": self.approver,
            "message": self.message,
            "channel": self.channel,
            "guild": self.guild,
            "maybe": self.maybe,
            "start": int(self.start.timestamp()) if self.start is not None else None,
            "select_options": self.select_options,
        }


class ValidImage(Converter):
    async def convert(self, ctx, argument):
        search = IMAGE_LINKS.search(argument)
        if not search:
            raise BadArgument(_("That's not a valid image link."))
        else:
            return argument
