import asyncio

from datetime import datetime
from statistics import median
from threading import active_count
from os import system
from sys import platform
from random import uniform
from collections import deque
from concurrent.futures import CancelledError

from aiopogo.hash_server import HashServer
from sqlalchemy.exc import OperationalError

import time

try:
    import _thread
except ImportError:
    import _dummy_thread as _thread

from .db import SIGHTING_CACHE, MYSTERY_CACHE
from .utils import get_current_hour, dump_pickle, get_start_coords, get_bootstrap_points, randomize_point
from .shared import get_logger, LOOP, run_threaded, ACCOUNTS
from .db_proc import DB_PROC
from .spawns import SPAWNS
from . import sanitized as conf
from .worker import Worker

BAD_STATUSES = (
    'FAILED LOGIN',
    'EXCEPTION',
    'NOT AUTHENTICATED',
    'KEY EXPIRED',
    'HASHING OFFLINE',
    'NIANTIC OFFLINE',
    'BAD REQUEST',
    'INVALID REQUEST',
    'CAPTCHA',
    'BANNED',
    'BENCHING',
    'REMOVING',
    'IP BANNED',
    'MALFORMED RESPONSE',
    'AIOPOGO ERROR',
    'MAX RETRIES',
    'HASHING ERROR',
    'PROXY ERROR',
    'TIMEOUT'
)

START_TIME = time.monotonic()


class Overseer:
    def __init__(self, manager):
        self.log = get_logger('overseer')
        self.workers = []
        self.manager = manager
        self.count = conf.GRID[0] * conf.GRID[1]
        self.start_date = datetime.now()
        self.things_count = deque(maxlen=9)
        self.paused = False
        self.coroutines_count = 0
        self.skipped = 0
        self.visits = 0
        self.mysteries = deque()
        self.coroutine_semaphore = asyncio.Semaphore(conf.COROUTINES_LIMIT, loop=LOOP)
        self.redundant = 0
        self.running = True
        self.all_seen = False
        self.idle_seconds = 0
        if platform == 'win32':
            self.clear = 'cls'
        else:
            self.clear = 'clear'
        self.log.info('Overseer initialized')

    def start(self, status_bar):
        self.captcha_queue = self.manager.captcha_queue()
        Worker.captcha_queue = self.manager.captcha_queue()
        self.extra_queue = self.manager.extra_queue()
        Worker.extra_queue = self.manager.extra_queue()
        if conf.MAP_WORKERS:
            Worker.worker_dict = self.manager.worker_dict()

        for username, account in ACCOUNTS.items():
            account['username'] = username
            if account.get('banned'):
                continue
            if account.get('captcha'):
                self.captcha_queue.put(account)
            else:
                self.extra_queue.put(account)

        self.workers = tuple(Worker(worker_no=x) for x in range(self.count))
        DB_PROC.start()
        LOOP.call_later(10, self.update_count)
        LOOP.call_later(max(conf.SWAP_OLDEST, conf.MINIMUM_RUNTIME), self.swap_oldest)
        LOOP.call_soon(self.update_stats)
        if status_bar:
            LOOP.call_soon(self.print_status)

    def update_count(self):
        self.things_count.append(str(DB_PROC.count))
        LOOP.call_later(10, self.update_count)

    def swap_oldest(self):
        if not self.paused and not self.extra_queue.empty():
            oldest, minutes = self.longest_running()
            if minutes > conf.MINIMUM_RUNTIME:
                LOOP.create_task(oldest.lock_and_swap(minutes))
        LOOP.call_later(conf.SWAP_OLDEST, self.swap_oldest)

    def update_stats(self):
        self.seen_stats, self.visit_stats, self.delay_stats, self.speed_stats = self.get_visit_stats()
        self.update_coroutines_count()
        LOOP.call_later(conf.STAT_REFRESH, self.update_stats)

    def print_status(self):
        try:
            system(self.clear)
            print(self.get_status_message())
            if self.running:
                LOOP.call_later(conf.REFRESH_RATE, self.print_status)
        except CancelledError:
            return
        except Exception as e:
            self.log.exception('{} occurred while printing status.', e.__class__.__name__)

    async def exit_progress(self):
        while self.coroutines_count > 2:
            try:
                self.update_coroutines_count()
                pending = DB_PROC.queue.qsize()
                # Spaces at the end are important, as they clear previously printed
                # output - \r doesn't clean whole line
                print(
                    '{} coroutines active, {} DB items pending   '.format(
                        self.coroutines_count, pending),
                    end='\r'
                )
                await asyncio.sleep(.5)
            except CancelledError:
                return
            except Exception as e:
                self.log.exception('A wild {} appeared in exit_progress!', e.__class__.__name__)

    @staticmethod
    def generate_stats(somelist):
        return {
            'max': max(somelist),
            'min': min(somelist),
            'med': median(somelist)
        }

    def get_visit_stats(self):
        visits = []
        seconds_since_start = time.monotonic() - START_TIME - self.idle_seconds
        hours_since_start = seconds_since_start / 3600
        seconds_per_visit = []
        seen_per_worker = []
        after_spawns = []
        speeds = []

        for w in self.workers:
            if w.after_spawn:
                after_spawns.append(w.after_spawn)
            seen_per_worker.append(w.total_seen)
            visits.append(w.visits)
            speeds.append(w.speed)
        if after_spawns:
            delay_stats = self.generate_stats(after_spawns)
        else:
            delay_stats = {'min': 0, 'max': 0, 'med': 0}
        seen_stats = self.generate_stats(seen_per_worker)
        visit_stats = self.generate_stats(visits)
        speed_stats = self.generate_stats(speeds)
        return seen_stats, visit_stats, delay_stats, speed_stats

    def get_dots_and_messages(self):
        """Returns status dots and status messages for workers

        Dots meaning:
        . = visited more than a minute ago
        , = visited less than a minute ago, no pokemon seen
        0 = visited less than a minute ago, no pokemon or forts seen
        : = visited less than a minute ago, pokemon seen
        ! = currently visiting
        | = cleaning bag
        $ = spinning a PokéStop
        * = sending a notification
        ~ = encountering a Pokémon
        I = initial, haven't done anything yet
        » = waiting to log in (limited by SIMULTANEOUS_LOGINS)
        ° = waiting to start app simulation (limited by SIMULTANEOUS_SIMULATION)
        ∞ = bootstrapping
        L = logging in
        A = simulating app startup
        T = completing the tutorial
        X = something bad happened
        H = waiting for the next period on the hashing server
        C = CAPTCHA

        Other letters: various errors and procedures
        """
        dots = []
        messages = []
        row = []
        for i, worker in enumerate(self.workers):
            if i > 0 and i % conf.GRID[1] == 0:
                dots.append(row)
                row = []
            if worker.error_code in BAD_STATUSES:
                row.append('X')
                messages.append(worker.status.ljust(20))
            elif worker.error_code:
                row.append(worker.error_code[0])
            else:
                row.append('.')
        if row:
            dots.append(row)
        return dots, messages

    def update_coroutines_count(self):
        try:
            tasks = asyncio.Task.all_tasks(LOOP)
            if self.running:
                self.coroutines_count = len(tasks)
            else:
                self.coroutines_count = sum(not t.done() for t in tasks)
        except RuntimeError:
            # Set changed size during iteration
            self.coroutines_count = '-1'

    def get_status_message(self):
        running_for = datetime.now() - self.start_date

        seconds_since_start = time.monotonic() - START_TIME - self.idle_seconds
        hours_since_start = seconds_since_start / 3600
        visits_per_second = self.visits / seconds_since_start

        output = [
            'Monocle running for {}'.format(running_for),
            'Known spawns: {}, unknown: {}, more: {}'.format(
                len(SPAWNS),
                SPAWNS.mysteries_count,
                SPAWNS.cells_count),
            '{} workers, {} threads, {} coroutines'.format(
                self.count,
                active_count(),
                self.coroutines_count),
            'DB queue: {}, sightings cache: {}, mystery cache: {}'.format(
                DB_PROC.queue.qsize(),
                len(SIGHTING_CACHE.store),
                len(MYSTERY_CACHE.store)),
            '',
            'Seen per worker: min {min}, max {max}, med {med:.0f}'.format(
                **self.seen_stats),
            'Visits per worker: min {min}, max {max:}, med {med:.0f}'.format(
                **self.visit_stats),
            'Visit delay: min {min:.1f}, max {max:.1f}, med {med:.1f}'.format(
                **self.delay_stats),
            'Speed: min {min:.1f}, max {max:.1f}, med {med:.1f}'.format(
                **self.speed_stats),
            'Extra accounts: {}, CAPTCHAs needed: {}'.format(
                self.extra_queue.qsize(),
                self.captcha_queue.qsize()),
            '',
            'Pokemon found count (10s interval):',
            ' '.join(self.things_count),
            '',
            'Visits: {}, per second: {:.2f}'.format(
                self.visits,
                visits_per_second),
            'Skipped: {}, unnecessary: {}'.format(
                self.skipped,
                self.redundant)
        ]

        try:
            seen = Worker.g['seen']
            captchas = Worker.g['captchas']
            output.append('Seen per visit: {v:.2f}, per minute: {m:.0f}'.format(
                v=seen / self.visits, m=seen / (seconds_since_start / 60)))

            if captchas:
                captchas_per_request = captchas / (self.visits / 1000)
                captchas_per_hour = captchas / hours_since_start
                output.append(
                    'CAPTCHAs per 1K visits: {r:.1f}, per hour: {h:.1f}, total: {t:d}'.format(
                    r=captchas_per_request, h=captchas_per_hour, t=captchas))
        except ZeroDivisionError:
            pass

        if conf.HASH_KEY:
            try:
                refresh = HashServer.status.get('period') - time.time()
                output.append('Hashes: {r}/{m}, refresh in {t:.0f}'.format(
                    r=HashServer.status.get('remaining'),
                    m=HashServer.status.get('maximum'),
                    t=refresh
                ))
            except TypeError:
                pass

        try:
            output.append('Notifications sent: {}, per hour {:.1f}'.format(
                Worker.notifier.sent, Worker.notifier.sent / hours_since_start))
        except AttributeError:
            pass

        output.append('')
        if not self.all_seen:
            no_sightings = ', '.join(str(w.worker_no)
                                     for w in self.workers
                                     if w.total_seen == 0)
            if no_sightings:
                output += ['Workers without sightings so far:', no_sightings, '']
            else:
                self.all_seen = True

        dots, messages = self.get_dots_and_messages()
        output += [' '.join(row) for row in dots]
        previous = 0
        for i in range(4, len(messages) + 4, 4):
            output.append('\t'.join(messages[previous:i]))
            previous = i
        if self.paused:
            output += ('', 'CAPTCHAs are needed to proceed.')
        return '\n'.join(output)

    def longest_running(self):
        workers = (x for x in self.workers if x.start_time)
        worker = next(workers)
        earliest = worker.start_time
        for w in workers:
            if w.start_time < earliest:
                worker = w
                earliest = w.start_time
        minutes = ((time.time() * 1000) - earliest) / 60000
        return worker, minutes

    def get_start_point(self):
        smallest_diff = float('inf')
        now = time.time() % 3600
        closest = None

        for spawn_id, spawn in SPAWNS.items():
            time_diff = now - spawn[1]
            if 0 < time_diff < smallest_diff:
                smallest_diff = time_diff
                closest = spawn_id
            if smallest_diff < 3:
                break
        return closest

    async def launch(self, bootstrap, pickle):
        exceptions = 0
        try:
            await self._launch(bootstrap, pickle)
        except CancelledError:
            return
        except Exception:
            exceptions += 1
            if exceptions > 100:
                self.log.exception('Over 100 errors occured in launcher loop, exiting.')
                return False
            else:
                self.log.exception('Error occured in launcher loop.')

    async def _launch(self, bootstrap, pickle):
        initial = True
        while True:
            if not initial:
                pickle = False
                bootstrap = False

            while True:
                try:
                    await run_threaded(SPAWNS.update, pickle)
                except OperationalError as e:
                    self.log.exception('Operational error while trying to update spawns.')
                    if initial:
                        raise OperationalError('Could not update spawns, ensure your DB is set up.') from e
                    await asyncio.sleep(20, loop=LOOP)
                except CancelledError:
                    raise
                except Exception as e:
                    self.log.exception('A wild {} appeared while updating spawns!', e.__class__.__name__)
                    await asyncio.sleep(20, loop=LOOP)
                else:
                    break

            if not SPAWNS or bootstrap:
                await self.bootstrap()

            current_hour = get_current_hour()
            if SPAWNS.after_last():
                current_hour += 3600
                initial = False

            if initial:
                start_point = self.get_start_point()
                if not start_point:
                    initial = False
            else:
                await run_threaded(dump_pickle, 'accounts', ACCOUNTS)

            for spawn_id, spawn in SPAWNS.items():
                if initial:
                    if spawn_id == start_point:
                        initial = False
                    else:
                        continue

                try:
                    if self.captcha_queue.qsize() > conf.MAX_CAPTCHAS:
                        self.paused = True
                        self.idle_seconds += await run_threaded(self.captcha_queue.full_wait, conf.MAX_CAPTCHAS)
                        self.paused = False
                except (EOFError, BrokenPipeError, FileNotFoundError):
                    continue

                point = spawn[0]
                spawn_time = spawn[1] + current_hour

                # negative = hasn't happened yet
                # positive = already happened
                time_diff = time.time() - spawn_time

                while time_diff < 0:
                    try:
                        mystery_point = self.mysteries.popleft()

                        await self.coroutine_semaphore.acquire()
                        LOOP.create_task(self.try_point(mystery_point))
                    except IndexError:
                        self.mysteries = SPAWNS.get_mysteries()
                        if not self.mysteries:
                            time_diff = time.time() - spawn_time
                            break
                    time_diff = time.time() - spawn_time

                if time_diff < -1:
                    await asyncio.sleep(time_diff * -1, loop=LOOP)
                elif time_diff > 5 and spawn_id in SIGHTING_CACHE.store:
                    self.redundant += 1
                    continue
                elif time_diff > conf.SKIP_SPAWN:
                    self.skipped += 1
                    continue

                await self.coroutine_semaphore.acquire()
                LOOP.create_task(self.try_point(point, spawn_time))

    async def bootstrap(self):
        try:
            await self.bootstrap_one()
            await asyncio.sleep(15, loop=LOOP)
        except CancelledError:
            raise
        except Exception:
            self.log.exception('An exception occurred during bootstrap phase 1.')

        try:
            self.log.warning('Starting bootstrap phase 2.')
            await self.bootstrap_two()
            self.log.warning('Finished bootstrapping.')
        except CancelledError:
            raise
        except Exception:
            self.log.exception('An exception occurred during bootstrap phase 2.')

    async def bootstrap_one(self):
        async def visit_release(worker, point):
            async with self.coroutine_semaphore:
                async with worker.busy:
                    if await worker.bootstrap_visit(point):
                        self.visits += 1

        for worker in self.workers:
            number = worker.worker_no
            worker.bootstrap = True
            point = get_start_coords(number)
            await asyncio.sleep(.25, loop=LOOP)
            LOOP.create_task(visit_release(worker, point))

    async def bootstrap_two(self):
        async def bootstrap_try(point):
            async with self.coroutine_semaphore:
                worker = await self.best_worker(point, must_visit=True)
                async with worker.busy:
                    if await worker.bootstrap_visit(point):
                        self.visits += 1

        tasks = (bootstrap_try(x) for x in get_bootstrap_points())
        await asyncio.gather(*tasks, loop=LOOP)

    async def try_point(self, point, spawn_time=None):
        try:
            point = randomize_point(point)
            worker = await self.best_worker(point, spawn_time)
            if not worker:
                if spawn_time:
                    self.skipped += 1
                else:
                    self.mysteries.append(point)
                return
            async with worker.busy:
                if spawn_time:
                    worker.after_spawn = time.time() - spawn_time

                if await worker.visit(point):
                    self.visits += 1
        except CancelledError:
            raise
        except Exception:
            self.log.exception('An exception occurred in try_point')
        finally:
            self.coroutine_semaphore.release()

    async def best_worker(self, point, spawn_time=None, must_visit=False):
        if spawn_time:
            skip_time = max(time.monotonic() + conf.GIVE_UP_KNOWN, spawn_time)
        elif must_visit:
            skip_time = float('inf')
        else:
            skip_time = time.monotonic() + conf.GIVE_UP_UNKNOWN

        while self.running:
            speed = None
            lowest_speed = float('inf')
            for w in (x for x in self.workers if not x.busy.locked()):
                speed = w.travel_speed(point)
                if speed < lowest_speed:
                    lowest_speed = speed
                    worker = w
                    if conf.GOOD_ENOUGH and speed < conf.GOOD_ENOUGH:
                        break
            if lowest_speed < conf.SPEED_LIMIT:
                worker.speed = lowest_speed
                return worker
            if time.monotonic() > skip_time:
                return None
            worker = None
            await asyncio.sleep(conf.SEARCH_SLEEP, loop=LOOP)

    def refresh_dict(self):
        while not self.extra_queue.empty():
            account = self.extra_queue.get()
            username = account['username']
            ACCOUNTS[username] = account
