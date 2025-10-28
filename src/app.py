import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, time, datetime, timedelta
from typing import List, Optional, Any

import caldav
import telegram
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
from pytz import timezone
from telegram.constants import ParseMode

DEFAULT_LOG_LEVEL = 'INFO'
log_level = os.environ.get('LOG_LEVEL', DEFAULT_LOG_LEVEL)
logging_mapping = logging.getLevelNamesMapping()
if log_level in logging_mapping:
    logging.basicConfig(format='%(asctime)s - %(message)s', level=logging_mapping[log_level])
else:
    logging.basicConfig(format='%(asctime)s - %(message)s', level=logging_mapping[DEFAULT_LOG_LEVEL])
    logging.error(f'Invalid LogLevel: {log_level}')


class Config:
    """Configuration class for managing configuration settings."""

    def __init__(self):
        """Initialize configuration settings from environment variables or default values."""
        load_dotenv()

        self.CALDAV_URL = os.environ.get('CALDAV_URL', None)
        self.CALDAV_USERNAME = os.environ.get('CALDAV_USERNAME', None)
        self.CALDAV_PASSWORD = os.environ.get('CALDAV_PASSWORD', None)
        self.CALENDAR_IDS = os.environ.get('CALENDAR_IDS', None)
        self.SYNC_INTERVAL_IN_SEC = os.environ.get('SYNC_INTERVAL_IN_SEC', 1800)
        self.FETCH_EVENT_WINDOW_IN_DAYS = os.environ.get('FETCH_EVENT_WINDOW_IN_DAYS', 5)
        self.TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', None)
        self.TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', None)
        self.TIMEZONE = timezone(os.environ.get('TIMEZONE', 'UTC'))
        self.DEFAULT_EVENT_REMINDER_MINUTES = os.environ.get('DEFAULT_EVENT_REMINDER_MINUTES', None)

        self.CALENDAR_IDS = self.CALENDAR_IDS.split(";") if self.CALENDAR_IDS else None


@dataclass(order=True)
class Reminder:
    """Class representing a reminder."""
    dt: datetime
    valarm: caldav.vobject.base.Component = field(compare=False)
    vevent: caldav.vobject = field(compare=False)

    url: str = field(default='')

    @staticmethod
    def from_vevent(vevent: caldav.vobject, minutes: int, url: str) -> 'Reminder':
        dtstart: datetime = vevent.dtstart.value
        alarm_dt = dtstart - timedelta(minutes=minutes)
        return Reminder(dt=alarm_dt, valarm=None, vevent=vevent, url=url)


@dataclass()
class Event:
    """Class representing an event."""
    vevent: caldav.vobject
    reminders: List[Reminder] = field(default_factory=list, init=False)
    raw_event: Any = field(default=None)


class CaldavHandler:
    """Handler for interacting with CalDAV server."""

    def __init__(self, config: Config):
        """Initialize CalDAV handler instance."""
        self.principal = None
        self.dav_client = None
        self.config: Config = config

    def login(self, caldav_url: str, username: str, password: str) -> bool:
        """Create CalDAV client and login to the server."""
        logging.debug(f'Creating caldav client: {caldav_url=}, {username=}')

        # Initiating the client object will not cause any server communication,
        # so the credentials aren't validated.
        self.dav_client = caldav.DAVClient(
            url=caldav_url,
            username=username,
            password=password
        )

        # This will cause communication with the server.
        logging.debug('Fetching principal object')
        try:
            self.principal = self.dav_client.principal()
            return True
        except caldav.lib.error.AuthorizationError as e:
            logging.warning(f'{e}')
            return False

    def fetch_calendars(self) -> Optional[List[caldav.objects.Calendar]]:
        """Fetch the list of calendars from the server.
        :return: A list of calendars or None if not logged in.
        """
        if self.principal is None:
            logging.error('Cannot fetch calendars: Not logged in')
            return None

        logging.debug('Fetching calendars')
        calendars = self.principal.calendars()

        if logging.getLogger().level == logging.DEBUG:
            logged_calendar = ''
            for calendar in calendars:
                logged_calendar += f'\t{calendar.name} ({calendar.id}): {calendar.url}\n'

            logging.debug(f'Fetched calendars:\n{logged_calendar}')

        return calendars

    def fetch_events(self, calendars: List[caldav.objects.Calendar]) -> List[Event]:
        """Fetch the events from the specified calendars.
        :param calendars: List of Calendar objects to fetch events from.
        :return: A list of Event objects.
        """
        logging.debug(f'Fetching events for {[cal.id for cal in calendars]}')
        eventsData: List[Event] = []
        for item in calendars:
            calendar = self.dav_client.calendar(url=item.url)
            start = datetime.now(tz=self.config.TIMEZONE)
            end = datetime.now(tz=self.config.TIMEZONE) + \
                  relativedelta(days=int(self.config.FETCH_EVENT_WINDOW_IN_DAYS))
            logging.debug(f'Searching for events. Range: [{str(start)}, {str(end)}]')

            events = calendar.search(
                event=True,
                expand=True,
                start=start,
                end=end,
            )

            for dav_event in events:
                vevent = dav_event.vobject_instance.vevent
                logging.debug(
                    f'Processing event: {vevent.summary.value} (id: {vevent.uid.value}, dtstart: {vevent.dtstart.value})')

                dtstart = vevent.dtstart.value

                if type(dtstart) == date:
                    dtstart = datetime.combine(dtstart, time(0, 0))
                    logging.debug(f'All-Day Event. Start-Time added: {dtstart}')

                if (dtstart.tzinfo is None or dtstart.tzinfo.utcoffset(dtstart) is None):
                    dtstart = self.config.TIMEZONE.localize(dtstart)
                    logging.debug(f'Timezone added to dtstart: {dtstart}')

                vevent.dtstart.value = dtstart.astimezone(self.config.TIMEZONE)
                dtstart = vevent.dtstart.value
                event = Event(vevent=vevent, raw_event=dav_event)

                for valarm in vevent.components():
                    trigger = valarm.trigger.value
                    logging.debug(f'Found reminder: {trigger} ({type(trigger)})')
                    if isinstance(trigger, timedelta):
                        alarm_dt = dtstart + trigger
                    else:
                        alarm_dt = trigger
                    event.reminders.append(
                        Reminder(dt=alarm_dt, valarm=valarm, vevent=vevent, url=dav_event.canonical_url))

                eventsData.append(event)

        if logging.getLogger().level == logging.DEBUG:
            logged_events = ''
            for event in eventsData:
                logged_events += \
                    f'\t{event.vevent.summary.value} ({event.vevent.uid.value}): {event.vevent.dtstart.value}\n'

            logging.debug(f'Fetched events({len(eventsData)}):\n{logged_events}')
        return eventsData

    def extract_reminders(self, events: List[Event]) -> List[Reminder]:
        """Extract reminders from the fetched events.
        :param events: List of Event objects to extract reminders from.
        :return: A list of Reminder objects.
        """
        logging.debug(f'Extracting Reminders from events: {[event.vevent.uid.value for event in events]}')
        reminders: List[Reminder] = []
        for event in events:
            if len(event.reminders) == 0 and self.config.DEFAULT_EVENT_REMINDER_MINUTES:
                logging.debug(f'Adding default reminder for event: {event.vevent.summary.value}')
                reminder = Reminder.from_vevent(vevent=event.vevent,
                                                minutes=int(self.config.DEFAULT_EVENT_REMINDER_MINUTES),
                                                url=event.raw_event.canonical_url)
                if reminder.dt >= datetime.now(tz=self.config.TIMEZONE):
                    reminders.append(reminder)
                continue
            for reminder in event.reminders:
                if reminder.dt >= datetime.now(tz=self.config.TIMEZONE):
                    reminders.append(reminder)
        reminders.sort()
        if logging.getLogger().level == logging.DEBUG:
            logged_events = ''
            for reminder in reminders:
                logged_events += f'\t{reminder.vevent.summary.value}: {reminder.dt}\n'

            logging.debug(f'Extracted reminders:\n{logged_events}')
        return reminders


class Worker:
    """Worker class for managing reminders and synchronization."""

    def __init__(self, config: Config, calHandler: CaldavHandler):
        """Initialize Worker instance with CaldavHandler."""
        self.sorted_reminders: List[Reminder] = []
        self.reminder_task: asyncio.Task = None
        self.calHandler: CaldavHandler = calHandler
        self.config: Config = config
        self.next_sync_dt = datetime.now(tz=self.config.TIMEZONE)
        self.cals: List[caldav.objects.Calendar] = None
        self.event_loop = asyncio.new_event_loop()

    async def run_at(self, dt, coro):
        """Run the specified coroutine at the specified datetime."""
        try:
            now = datetime.now(tz=self.config.TIMEZONE)
            await asyncio.sleep((dt - now).total_seconds())
            return await coro()
        except asyncio.CancelledError:
            pass

    def run(self):
        """Run the main event loop for synchronization and reminder processing."""
        self.event_loop.create_task(self.sync())
        self.event_loop.run_forever()

    def scheduleReminderTask(self):
        """Schedule the next reminder task based on the sorted reminders list."""
        if self.reminder_task:
            self.reminder_task.cancel()

        if len(self.sorted_reminders) > 0:
            self.reminder_task = self.event_loop.create_task(
                self.run_at(self.sorted_reminders[0].dt, self.process_reminders))

    async def sync(self) -> None:
        """Synchronize calendars and reminders with the server."""
        logging.info('Syncing...')
        try:
            if self.cals is None:
                self.cals = self.calHandler.fetch_calendars()
                if self.cals is None:
                    logging.error('Cannot sync calendar')
                    return

            cals_subscripted = list(filter(lambda x: x.id in self.config.CALENDAR_IDS, self.cals))
            events = self.calHandler.fetch_events(cals_subscripted)
            if events:
                sorted_reminders_new = self.calHandler.extract_reminders(events)
                if sorted_reminders_new != self.sorted_reminders:
                    self.sorted_reminders = sorted_reminders_new
                    self.scheduleReminderTask()

        except Exception as e:
            logging.error(f'Exception occured')
            logging.exception(e)
        finally:
            next_sync_dt = datetime.now(tz=self.config.TIMEZONE) + \
                           relativedelta(seconds=int(self.config.SYNC_INTERVAL_IN_SEC))

            loop = asyncio.get_event_loop()
            loop.create_task(self.run_at(next_sync_dt, self.sync))

    async def process_reminders(self):
        """Process reminders and send notifications."""
        self.reminder_task = None
        try:
            logging.debug('Processing reminders')
            while await self.process_next_reminder():
                pass
            self.scheduleReminderTask()
        except asyncio.CancelledError:
            logging.debug('cancel processing reminders')
            pass

    async def process_next_reminder(self):
        """Process the next reminder in the sorted reminders list."""
        if len(self.sorted_reminders) > 0:
            reminder = self.sorted_reminders.pop(0)
            if reminder.dt <= datetime.now(tz=self.config.TIMEZONE):
                logging.info(f'Sending reminder for {reminder.vevent.summary.value}')
                bot = telegram.Bot(self.config.TELEGRAM_BOT_TOKEN)
                await bot.send_message(text=self.get_bot_message(reminder),
                                       chat_id=self.config.TELEGRAM_CHAT_ID, parse_mode=ParseMode.HTML)
                return True
        return False

    def get_bot_message(self, reminder: Reminder):
        # Check if the template file exists
        template_path = 'template.html'

        def format_date(value, format="%d.%m.%Y %H:%M:%S"):
            """ Custom filter to format datetime"""
            if value:
                return value.strftime(format)
            return ''

        if os.path.exists(template_path):
            def remove_empty_lines(input_string):
                # Split the string into lines, filter out empty lines, and join the lines back into a string
                return '\n'.join(line for line in input_string.splitlines() if line.strip())

            # Load and render the template using Jinja2
            env = Environment(loader=FileSystemLoader(searchpath='./'))
            template = env.get_template('template.html')
            env.filters['format_date'] = format_date

            # Render the template with the provided variables
            msg = template.render(
                summary=reminder.vevent.contents.get("summary", [None])[
                    0].value if "summary" in reminder.vevent.contents else "",
                description=reminder.vevent.contents.get("description", [None])[
                    0].value if "description" in reminder.vevent.contents else "",
                location=reminder.vevent.contents.get("location", [None])[
                    0].value if "location" in reminder.vevent.contents else "",
                date=reminder.vevent.contents.get("dtstart", [None])[
                    0].value if "dtstart" in reminder.vevent.contents else "",
                url=reminder.url,
            ).strip()
            return remove_empty_lines(msg)
        return f'<b>{reminder.vevent.summary.value}</b>\r\n{format_date(reminder.vevent.dtstart.value)}'


if __name__ == '__main__':
    """Main entry point for the script."""
    config = Config()

    if config.CALDAV_URL is None:
        logging.error('Cannot start. CALDAV_URL not set.')
        sys.exit(1)

    if config.CALDAV_USERNAME is None or config.CALDAV_PASSWORD is None:
        logging.error('Cannot start. CALDAV_USERNAME or CALDAV_PASSWORD not set.')
        sys.exit(1)

    if config.TELEGRAM_BOT_TOKEN is None or config.TELEGRAM_CHAT_ID is None:
        logging.error('Cannot start. TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.')
        sys.exit(1)

    caldav_handler = CaldavHandler(config)
    result = caldav_handler.login(caldav_url=config.CALDAV_URL, username=config.CALDAV_USERNAME,
                                  password=config.CALDAV_PASSWORD)
    if result is False:
        logging.error('Cannot start: Login failed')
        sys.exit(1)
    worker = Worker(config, caldav_handler)
    worker.run()
