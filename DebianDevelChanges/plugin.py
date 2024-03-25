#
#   Debian Changes Bot
#   Copyright (C) 2008 Chris Lamb <chris@chris-lamb.co.uk>
#   Copyright (C) 2015-2020 Sebastian Ramacher <sramacher@debian.org>
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of the
#   License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU Affero General Public License for more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import os.path
import re
import time
import supybot
import threading
import requests
import shutil
from enum import Enum

from supybot import ircdb, log, schedule
from supybot.commands import wrap, many

from DebianDevelChangesBot import DataSource, pseudo_packages
from DebianDevelChangesBot.mailparsers import get_message
from DebianDevelChangesBot.datasources import (
    TestingRCBugs,
    NewQueue,
    RmQueue,
    StableRCBugs,
    Dinstall,
    AptArchive,
    PseudoPackages,
)
from DebianDevelChangesBot.utils import (
    parse_mail,
    colourise,
    rewrite_topic,
    madison,
    format_email_address,
    popcon,
)
from DebianDevelChangesBot.utils.decoding import split_address


def schedule_remove_event(event):
    try:
        schedule.removeEvent(event)
    except KeyError:
        pass


def schedule_remove_periodic_event(event):
    try:
        schedule.removePeriodicEvent(event)
    except KeyError:
        pass


class ProcessingResult(Enum):
    ACTION = 2
    NO_ACTION = 1
    ERROR = 0


class DebianDevelChanges(supybot.callbacks.Plugin):
    threaded = True

    def __init__(self, irc):
        super().__init__(irc)
        self.irc = irc
        self.topic_lock = threading.Lock()
        self.mail_lock = threading.Lock()

        self.requests_session = requests.Session()
        self.requests_session.verify = True

        self.queued_topics = {}
        self.last_n_messages = []

        # data sources
        pseudo_packages.pp = PseudoPackages(self.requests_session)
        self.pseudo_packages = pseudo_packages.pp
        self.stable_rc_bugs = StableRCBugs(self.requests_session)
        self.testing_rc_bugs = TestingRCBugs(self.requests_session)
        self.new_queue = NewQueue(self.requests_session)
        self.dinstall = Dinstall(self.requests_session)
        self.rm_queue = RmQueue(self.requests_session)
        self.apt_archive = AptArchive(
            self.registryValue("apt_configuration_directory"),
            self.registryValue("apt_cache_directory"),
        )
        self.data_sources = (
            self.pseudo_packages,
            self.stable_rc_bugs,
            self.testing_rc_bugs,
            self.new_queue,
            self.dinstall,
            self.rm_queue,
            self.apt_archive,
        )

        # Schedule datasource updates
        def wrapper(source):
            def implementation():
                try:
                    source.update()
                except Exception as e:
                    log.exception(f"Failed to update {source.NAME}: {e}")
                self._topic_callback()

            return implementation

        for source in self.data_sources:
            # schedule periodic events
            schedule.addPeriodicEvent(
                wrapper(source), source.INTERVAL, source.NAME, now=False
            )
            # and run them now once
            schedule.addEvent(wrapper(source), time.time() + 1)

        # Schedule mail update
        self._inject_maildir = os.path.expanduser("~/inject")
        if not os.path.isdir(self._inject_maildir):
            os.mkdir(self._inject_maildir)
        self._failed_maildir = os.path.expanduser("~/failed-mails")
        if not os.path.isdir(self._failed_maildir):
            os.mkdir(self._failed_maildir)
        self._processed_maildir = os.path.expanduser("~/processed-mails")
        if not os.path.isdir(self._processed_maildir):
            os.mkdir(self._processed_maildir)

        schedule.addPeriodicEvent(self._email_callback, 60, "process-mail", now=False)

        # Schedule rejoins
        schedule.addPeriodicEvent(self._rejoin_channels, 600, "rejoin", now=False)

    def die(self):
        schedule_remove_periodic_event("rejoin")
        schedule_remove_periodic_event("process-mail")
        for source in self.data_sources:
            schedule_remove_periodic_event(source.NAME)

        super().die()

    def _rejoin_channels(self):
        for channel in supybot.conf.supybot.networks.get(self.irc.network).get(
            "channels"
        ):
            log.info(f"Checking if {channel} joined")
            if channel in self.irc.state.channels:
                continue

            log.info(f"Rejoining {channel}")
            self.irc.queueMsg(supybot.ircmsgs.join(channel))

    def _email_callback(self):
        # make sure that we only process from one thread
        if self.mail_lock.locked():
            return

        with self.mail_lock:
            for mail in os.listdir(self._inject_maildir):
                mail = os.path.join(self._inject_maildir, mail)
                if not os.path.isfile(mail):
                    continue

                log.info(f"Processing mail {mail}")
                with open(mail, mode="rb") as fileobj:
                    res = self._process_mail(fileobj)
                if res == ProcessingResult.ACTION:
                    # store mails that caused an action (for 7 days)
                    log.info(
                        f"Mail {mail} caused action, storing in {self._processed_maildir}"
                    )
                    shutil.move(mail, self._processed_maildir)
                elif res == ProcessingResult.NO_ACTION:
                    # remove mails that caused no action
                    log.info(f"Mail {mail} caused no action")
                    os.unlink(mail)
                else:
                    # store mails that failed to be processed
                    log.info(
                        f"Processing mail {mail} failed, storing in {self._failed_maildir}"
                    )
                    shutil.move(mail, self._failed_maildir)

    def _process_mail(self, fileobj):
        try:
            emailmsg = parse_mail(fileobj)
            msg = get_message(emailmsg, new_queue=self.new_queue)
            if not msg:
                return ProcessingResult.NO_ACTION

            txt = colourise(msg.for_irc())

            # Simple flood/duplicate detection
            if txt in self.last_n_messages:
                return ProcessingResult.NO_ACTION
            self.last_n_messages.insert(0, txt)
            self.last_n_messages = self.last_n_messages[:20]

            packages = [package.strip() for package in msg.package.split(",")]

            maintainer_info = None
            if hasattr(msg, "maintainer"):
                maintainer_info = (split_address(msg.maintainer),)
            else:
                maintainer_info = []
                for package in packages:
                    try:
                        maintainer_info.append(self.apt_archive.get_maintainer(package))
                    except DataSource.DataError as e:
                        log.info(f"Failed to query maintainer for {package}: {e}")

            for channel in self.irc.state.channels:
                # match package or nothing by default
                package_regex = self.registryValue("package_regex", channel) or "a^"
                package_match = False
                for package in packages:
                    package_match = re.search(package_regex, package)
                    if package_match:
                        break

                maintainer_match = False
                maintainer_regex = self.registryValue("maintainer_regex", channel)
                if (
                    maintainer_regex
                    and maintainer_info is not None
                    and len(maintainer_info) >= 0
                ):
                    for mi in maintainer_info:
                        maintainer_match = re.search(maintainer_regex, mi["email"])
                        if maintainer_match:
                            break

                if not package_match and not maintainer_match:
                    continue

                distribution_regex = self.registryValue("distribution_regex", channel)

                if distribution_regex:
                    if not hasattr(msg, "distribution"):
                        # If this channel has a distribution regex, don't
                        # bother continuing unless the message actually has a
                        # distribution. This filters security messages, etc.
                        continue

                    if not re.search(distribution_regex, msg.distribution):
                        # Distribution doesn't match regex; don't send this
                        # message.
                        continue

                send_privmsg = self.registryValue("send_privmsg", channel)
                # Send NOTICE per default and if 'send_privmsg' is set for the
                # channel, send PRIVMSG instead.
                if send_privmsg:
                    ircmsg = supybot.ircmsgs.privmsg(channel, txt)
                else:
                    ircmsg = supybot.ircmsgs.notice(channel, txt)

                self.irc.queueMsg(ircmsg)
        except Exception as e:
            log.exception(f"Uncaught exception: {e}")
            return ProcessingResult.ERROR

        return ProcessingResult.ACTION

    def _topic_callback(self):
        sections = {
            self.testing_rc_bugs.get_number_bugs: "RC bug count",
            self.stable_rc_bugs.get_number_bugs: "stable RC bug count",
            self.new_queue.get_size: "NEW queue",
            self.new_queue.get_backports_size: "backports NEW queue",
            self.rm_queue.get_size: "RM queue",
            self.dinstall.get_status: "dinstall",
        }

        channels = set()
        with self.topic_lock:
            values = {}
            for callback, prefix in sections.items():
                new_value = callback()
                if new_value is not None:
                    values[prefix] = new_value

            for channel in self.irc.state.channels:
                new_topic = topic = self.irc.state.getTopic(channel)

                for prefix, value in values.items():
                    new_topic = rewrite_topic(new_topic, prefix, value)

                if topic != new_topic:
                    self.queued_topics[channel] = new_topic

                    if channel not in channels:
                        log.info(
                            f"Queueing change of topic in {channel} to '{new_topic}'"
                        )
                        channels.add(channel)

        for channel in channels:
            event_name = f"{channel}_topic"
            schedule_remove_event(event_name)

            def update_topic(channel=channel):
                self._update_topic(channel)

            schedule.addEvent(update_topic, time.time() + 60, event_name)

    def _update_topic(self, channel):
        with self.topic_lock:
            try:
                new_topic = self.queued_topics[channel]
                log.info(f"Changing topic in {channel} to '{new_topic}'")
                self.irc.queueMsg(supybot.ircmsgs.topic(channel, new_topic))
            except KeyError:
                pass

    def rc(self, irc, msg, args):
        """Link to UDD RC bug overview."""
        num_bugs = self.testing_rc_bugs.get_number_bugs()
        if type(num_bugs) is int:
            irc.reply(
                f"There are {num_bugs} release-critical bugs in the testing distribution. See https://udd.debian.org/bugs.cgi?release=bullseye&notmain=ign&merged=ign&rc=1"
            )
        else:
            irc.reply("No data at this time.")

    rc = wrap(rc)
    bugs = wrap(rc)

    def update(self, irc, msg, args):
        """Trigger an update."""
        if not ircdb.checkCapability(msg.prefix, "owner"):
            irc.reply("You are not authorised to run this command.")
            return

        for source in self.data_sources:
            source.update()
            irc.reply(f"Updated {source.NAME}.")
        self._topic_callback()

    update = wrap(update)

    def madison(self, irc, msg, args, package):
        """List packages."""
        try:
            lines = madison(package)
            if not lines:
                irc.reply(f'Did not get a response -- is "{package}" a valid package?')
                return

            field_styles = ("package", "version", "distribution", "section")
            for line in lines:
                out = []
                fields = line.strip().split("|", len(field_styles))
                for style, data in zip(field_styles, fields):
                    out.append(f"[{style}]{data}")
                irc.reply(colourise("[reset]|".join(out)), prefixNick=False)
        except Exception as e:
            irc.reply("Error: %s" % e.message)

    madison = wrap(madison, ["text"])

    def get_pool_url(self, package):
        if package.startswith("lib"):
            return (package[:4], package)
        else:
            return (package[:1], package)

    def _maintainer(self, irc, msg, args, items):
        """Get maintainer for package."""
        for package in items:
            info = self.apt_archive.get_maintainer(package)
            if info:
                display_name = format_email_address(
                    f"{info['name']} <{info['email']}>", max_domain=18
                )

                login = info["email"]
                if login.endswith("@debian.org"):
                    login = login.replace("@debian.org", "")

                msg = f"[desc]Maintainer for[reset] [package]{package}[reset] [desc]is[reset] [by]{display_name}[reset]: [url]https://qa.debian.org/developer.php?login={login}[/url]"
            else:
                msg = f'Unknown source package "{package}"'

            irc.reply(colourise(msg), prefixNick=False)

    maintainer = wrap(_maintainer, [many("anything")])
    maint = wrap(_maintainer, [many("anything")])
    who_maintains = wrap(_maintainer, [many("anything")])

    def _qa(self, irc, msg, args, items):
        """Get link to QA page."""
        for package in items:
            url = "https://tracker.debian.org/pkg/" + package
            msg = (
                f"[desc]QA page for[reset] [package]{package}[reset]: [url]{url}[/url]"
            )
            irc.reply(colourise(msg), prefixNick=False)

    qa = wrap(_qa, [many("anything")])
    overview = wrap(_qa, [many("anything")])
    package = wrap(_qa, [many("anything")])
    pkg = wrap(_qa, [many("anything")])
    srcpkg = wrap(_qa, [many("anything")])

    def _changelog(self, irc, msg, args, items):
        """Get link to changelog."""
        for package in items:
            url = (
                "https://packages.debian.org/changelogs/pool/main/%s/%s/current/changelog"
                % self.get_pool_url(package)
            )
            msg = f"[desc]debian/changelog for[reset] [package]{package}[reset]: [url]{url}[/url]"
            irc.reply(colourise(msg), prefixNick=False)

    changelog = wrap(_changelog, [many("anything")])
    changes = wrap(_changelog, [many("anything")])

    def _copyright(self, irc, msg, args, items):
        """Link to copyright files."""
        for package in items:
            url = (
                "https://packages.debian.org/changelogs/pool/main/%s/%s/current/copyright"
                % self.get_pool_url(package)
            )
            msg = f"[desc]debian/copyright for[reset] [package]{package}[reset]: [url]{url}[/url]"
            irc.reply(colourise(msg), prefixNick=False)

    copyright = wrap(_copyright, [many("anything")])

    def _buggraph(self, irc, msg, args, items):
        """Link to bug graph."""
        for package in items:
            msg = f"[desc]Bug graph for[reset] [package]{package}[reset]: [url]https://qa.debian.org/data/bts/graphs/{package[0]}/{package}.png[/url]"
            irc.reply(colourise(msg), prefixNick=False)

    buggraph = wrap(_buggraph, [many("anything")])
    bug_graph = wrap(_buggraph, [many("anything")])

    def _buildd(self, irc, msg, args, items):
        """Link to buildd page."""
        for package in items:
            msg = f"[desc]buildd status for[reset] [package]{package}[reset]: [url]https://buildd.debian.org/pkg.cgi?pkg={package}[/url]"
            irc.reply(colourise(msg), prefixNick=False)

    buildd = wrap(_buildd, [many("anything")])

    def _popcon(self, irc, msg, args, package):
        """Get popcon data."""
        try:
            msg = popcon(package, self.requests_session)
            if msg:
                irc.reply(colourise(msg.for_irc()), prefixNick=False)
        except Exception as e:
            irc.reply(f"Error: unable to obtain popcon data for {package}")

    popcon = wrap(_popcon, ["text"])

    def _testing(self, irc, msg, args, items):
        """Check testing migration status."""
        for package in items:
            msg = f"[desc]Testing migration status for[reset] [package]{package}[reset]: [url]https://qa.debian.org/excuses.php?package={package}[/url]"
            irc.reply(colourise(msg), prefixNick=False)

    testing = wrap(_testing, [many("anything")])
    migration = wrap(_testing, [many("anything")])

    def _new(self, irc, msg, args):
        """Link to NEW queue."""
        size = self.new_queue.get_size()
        line = f"[desc]NEW queue is[reset]: [url]https://ftp-master.debian.org/new.html[/url]. [desc]Current size is:[reset] {size}"
        irc.reply(colourise(line))

    new = wrap(_new)
    new_queue = wrap(_new)
    newqueue = wrap(_new)


Class = DebianDevelChanges
