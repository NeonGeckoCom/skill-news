# NEON AI (TM) SOFTWARE, Software Development Kit & Application Framework
# All trademark and other rights reserved by their respective owners
# Copyright 2008-2022 Neongecko.com Inc.
# Contributors: Daniel McKnight, Guy Daniels, Elon Gasper, Richard Leeds,
# Regina Bloomstine, Casimiro Ferreira, Andrii Pernatii, Kirill Hrymailo
# BSD-3 License
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS  BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS;  OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE,  EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Copyright 2018 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import feedparser
import os
from os.path import join, abspath, dirname
import requests
import subprocess
import time
import traceback

from shutil import copyfile
from urllib.parse import quote
from neon_utils.message_utils import request_from_mobile
from adapt.intent import IntentBuilder
from neon_utils.skills.common_play_skill import CommonPlaySkill, CPSMatchLevel
from neon_utils.logger import LOG
from neon_utils.message_utils import get_message_user

from mycroft.skills.core import intent_handler, intent_file_handler
from mycroft.util import get_cache_directory
from mycroft.util.parse import fuzzy_match
from mycroft.audio import wait_while_speaking

from .stations.abc import get_abc_url as abc
from .stations.tsf import get_tsf_url as tsf
from .stations.gpb import get_gpb_url as gpb


def image_path(filename):
    return 'file://' + join(dirname(abspath(__file__)), 'images', filename)


# NOTE: This has to be in sync with the settingsmeta options
FEEDS = {
    'other': ('Your custom feed', None, None),
    'custom': ('Your custom feed', None, None),
    'ABC': ('ABC News Australia', abc, image_path('ABC.png')),
    'AP':  ('AP Hourly Radio News',
            "https://www.spreaker.com/show/1401466/episodes/feed",
            image_path('AP.png')),
    'BBC': ('BBC News', 'https://podcasts.files.bbci.co.uk/p02nq0gn.rss',
            image_path('BBC.png')),
    'CBC': ('CBC News',
            'https://www.cbc.ca/podcasting/includes/hourlynews.xml',
            image_path('CBC.png')),
    'DLF': ('DLF', 'https://www.deutschlandfunk.de/'
                   'podcast-nachrichten.1257.de.podcast.xml',
            image_path('DLF')),
    'Ekot': ('Ekot', 'https://api.sr.se/api/rss/pod/3795',
             image_path('Ekot.png')),
    'FOX': ('Fox News', 'http://feeds.foxnewsradio.com/FoxNewsRadio',
            image_path('FOX.png')),
    'NPR': ('NPR News Now', 'https://www.npr.org/rss/podcast.php?id=500005',
            image_path('NPR.png')),
    'PBS': ('PBS NewsHour', 'https://www.pbs.org/newshour/feeds/'
                            'rss/podcasts/show',
            image_path('PBS.png')),
    'VRT': ('VRT Nieuws', 'https://progressive-audio.lwc.vrtcdn.be/'
                          'content/fixed/11_11niws-snip_hi.mp3',
            None),
    'WDR': ('WDR', 'https://www1.wdr.de/mediathek/audio/'
                   'wdr-aktuell-news/wdr-aktuell-152.podcast',
            image_path('WDR')),
    'YLE': ('YLE', 'https://feeds.yle.fi/areena/v1/series/1-1440981.rss',
            image_path('Yle.png')),
    "GBP": ("Georgia Public Radio", gpb, None),
    "RDP": ("RDP Africa", "http://www.rtp.pt//play/itunes/5442", None),
    "RNE": ("National Spanish Radio",
            "http://api.rtve.es/api/programas/36019/audios.rs", None),
    "TSF": ("TSF Radio", tsf, None),
    "OE3": ("Ö3 Nachrichten",
            "https://oe3meta.orf.at/oe3mdata/StaticAudio/Nachrichten.mp3",
            None),
}


# If feed URL ends in specific filetype, just play it
DIRECT_PLAY_FILETYPES = ['.mp3']


def find_mime(url):
    mime = 'audio/mpeg'
    response = requests.Session().head(url, allow_redirects=True)
    if 200 <= response.status_code < 300:
        mime = response.headers['content-type']
    return mime


def contains_html(file):
    """Reads file and reports if a <html> tag is contained.

    Makes a temporary copy of the file to prevent locking downloads in
    progress. This should not be considered a robust method of testing if a
    file is a HTML document, but sufficient for this purpose.

    Args:
        file (str): path of file

    Returns:
        bool: whether a <html> tag was found
    """
    found_html = False
    tmp_file = '/tmp/mycroft-news-html-check'
    try:
        # Copy file to prevent locking larger file being downloaded
        copyfile(file, tmp_file)
        with open(tmp_file, mode='r', encoding="utf-8") as f:
            for line in f:
                if '<html>' in line:
                    found_html = True
                    break
    except Exception:
        LOG.debug('Could not parse file, assumed not to be HTML.')
    return found_html


class NewsSkill(CommonPlaySkill):
    def __init__(self):
        super().__init__(name="NewsSkill")
        self.curl = None
        self.now_playing = None
        self.user_to_last_message = {}
        self.stream = '{}/stream'.format(get_cache_directory('NewsSkill'))
        self.default_feed = None
        self.feeds = FEEDS
        self.alt_feed_names = None

    def initialize(self):
        time.sleep(1)
        LOG.debug('Disabling restart intent')
        self.disable_intent('restart_playback')
        # Default feed per country code, if user has not selected a default
        self.default_feed = self.translate_namedvalues('country.default')
        # Longer titles or alternative common names of feeds for searching
        self.alt_feed_names = self.translate_namedvalues('alt.feed.name')

    def CPS_match_query_phrase(self, phrase, message):
        matched_feed = {'key': None, 'conf': 0.0}

        # Remove "the" as it matches too well will "other"
        search_phrase = phrase.lower().replace('the', '')

        # Catch any short explicit phrases eg "play the news"
        news_phrases = self.translate_list("PlayTheNews") or []
        if search_phrase.strip() in news_phrases:
            station_key = self.preference_skill(message).get("station", "not_set")
            if station_key == "not_set":
                station_key = self.get_default_station()
            matched_feed = {'key': station_key, 'conf': 1.0}

        def match_feed_name(phrase, feed):
            """Determine confidence that a phrase requested a given feed.

            Args:
                phrase (str): utterance from the user
                feed (str): the station feed to match against

            Returns:
                tuple: feed being matched, confidence level
            """
            phrase = phrase.lower().replace("play", "")
            feed_short_name = feed.lower()
            short_name_confidence = fuzzy_match(phrase, feed_short_name)
            long_name_confidence = fuzzy_match(phrase, FEEDS[feed][0].lower())
            # Test with "News" added in case user only says acronym eg "ABC".
            # As it is short it may not provide a high match confidence.
            news_keyword = self.translate("OnlyNews").lower()
            modified_short_name = "{} {}".format(feed_short_name, news_keyword)
            variation_confidence = fuzzy_match(phrase, modified_short_name)
            key_confidence = 0.6 if news_keyword in phrase and feed_short_name in phrase else 0.0

            conf = max((short_name_confidence, long_name_confidence,
                        variation_confidence, key_confidence))
            return feed, conf

        # Check primary feed list for matches eg 'ABC'
        for feed in FEEDS:
            feed, conf = match_feed_name(search_phrase, feed)
            if conf > matched_feed['conf']:
                matched_feed['conf'] = conf
                matched_feed['key'] = feed

        # Check list of alternate names eg 'associated press' => 'AP'
        for name in self.alt_feed_names:
            conf = fuzzy_match(search_phrase, name)
            if conf > matched_feed['conf']:
                matched_feed['conf'] = conf
                matched_feed['key'] = self.alt_feed_names[name]

        # If no match but utterance contains news, return low confidence level
        if matched_feed['conf'] == 0.0 and self.voc_match(search_phrase, "News"):
            matched_feed = {'key': None, 'conf': 0.5}

        feed_title = FEEDS[matched_feed['key']][0]
        if matched_feed['conf'] >= 0.9:
            match_level = CPSMatchLevel.EXACT
        elif matched_feed['conf'] >= 0.7:
            match_level = CPSMatchLevel.ARTIST
        elif matched_feed['conf'] >= 0.5:
            match_level = CPSMatchLevel.CATEGORY
        else:
            match_level = None
            return match_level
        feed_data = {'feed': matched_feed['key']}

        return (feed_title, match_level, feed_data)

    def CPS_start(self, phrase, data, message=None):
        if data and data.get("feed"):
            # Play the requested news service
            self.handle_latest_news(message, feed=data["feed"])
        else:
            # Just use the default news feed
            self.handle_latest_news(message)

    def get_default_station(self):
        country_code = self.location['city']['state']['country']['code']
        if self.default_feed and self.default_feed.get(country_code) is not None:
            feed_code = self.default_feed[country_code]
        else:
            feed_code = "NPR"
        return feed_code

    def get_station(self, message):
        """Get station user selected from settings or default station.

        Fallback order:
        1. User selected station
        2. User defined custom url
        3. Default station for country
        """
        feed_code = self.preference_skill(message)["station"]
        station_url = self.preference_skill(message)["custom_url"]

        if feed_code in FEEDS:
            title, station_url, image = FEEDS[feed_code]
        elif len(station_url) > 0:
            title = FEEDS["custom"][0]
            image = None
        else:
            feed_code = self.get_default_station()
            title, station_url, image = FEEDS[feed_code]

        return title, station_url, image

    @staticmethod
    def get_media_url(station_url):
        media_url = None
        if callable(station_url):
            return station_url()

        # If link is an audio file, just play it.
        if station_url and station_url[-4:] in DIRECT_PLAY_FILETYPES:
            LOG.debug('Playing news from URL: {}'.format(station_url))
            return station_url

        # Otherwise it is an RSS or XML feed
        data = feedparser.parse(station_url.strip())
        # After the intro, find and start the news stream
        # select the first link to an audio file
        for link in data['entries'][0]['links']:
            if 'audio' in link['type']:
                media_url = link['href']
                break
            else:
                # fall back to using the first link in the entry
                media_url = data['entries'][0]['links'][0]['href']
        LOG.debug('Playing news from URL: {}'.format(media_url))
        # TODO - check on temporary workaround and remove - see issue #87
        if station_url.startswith('https://www.npr.org/'):
            media_url = media_url.split('?')[0]
        return media_url

    @intent_file_handler("PlayTheNews.intent")
    def handle_latest_news_alt(self, message):
        if self.neon_in_request(message):
            # Capture some alternative ways of requesting the news via Padatious
            utt = message.data["utterance"]
            match = self.CPS_match_query_phrase(utt, message)
            if match and len(match) > 2:
                feed = match[2]["feed"]
            else:
                feed = None
            if self.voc_match(utt, "News"):
                self.handle_latest_news(message, feed)

    @intent_handler(IntentBuilder("").one_of("Give", "Latest").require("News"))
    def handle_latest_news(self, message=None, feed=None):
        if self.neon_in_request(message):
            try:
                self.stop()
                # rss = None
                self.now_playing = None
                LOG.debug(feed)
                if feed and feed in self.feeds and feed != 'other':
                    self.now_playing, rss, image = self.feeds[feed]
                else:
                    self.now_playing, rss, image = self.get_station(message)

                # Speak intro while downloading in background
                self.speak_dialog('news', data={"from": self.now_playing})

                url = self.get_media_url(rss)
                if request_from_mobile(message):
                    pass
                    # TODO
                    # self.mobile_skill_intent("podcast", {"link": url}, message)
                    # self.socket_io_emit('podcast', f"&link={url}", flac_filename=message.context["flac_filename"])
                elif message.context.get('klat_data'):
                    self.speak(f"newsskill:{url}")
                    # self.send_with_audio("Here is the news.", url, message)
                else:
                    mime = find_mime(url)
                    # (Re)create Fifo
                    if os.path.exists(self.stream):
                        os.remove(self.stream)
                    os.mkfifo(self.stream)

                    LOG.debug('Running curl {}'.format(url))
                    args = ['curl', '-L', quote(url, safe=":/"), '-o', self.stream]
                    self.curl = subprocess.Popen(args)

                    # Show news title, if there is one
                    wait_while_speaking()
                    # Begin the news stream
                    LOG.info('Feed: {}'.format(feed))
                    LOG.debug(self.stream)
                    LOG.debug(mime)
                    self.CPS_play(('file://' + self.stream, mime))
                    self.CPS_send_status(image=image or image_path('generic.png'),
                                         track=self.now_playing)
                    self.user_to_last_message[get_message_user(message)] = message
                    self.enable_intent('restart_playback')

            except Exception as e:
                LOG.error("Error: {0}".format(e))
                LOG.info("Traceback: {}".format(traceback.format_exc()))
                self.speak_dialog("could.not.start.the.news.feed")

    @intent_handler(IntentBuilder('').require('Restart'))
    def restart_playback(self, message):
        LOG.debug('Restarting last message')
        if get_message_user(message) in self.user_to_last_message.keys():
            self.handle_latest_news(self.user_to_last_message[get_message_user(message)])

    def stop(self):
        # # End mpv playback
        # try:
        #     self.socket.send(b'{"command": ["quit"]}\n')
        #     self.socket.close()
        # except Exception as e:
        #     LOG.warning(e)

        # Disable restarting when stopped
        # if self.user_to_last_message:
        #     self.disable_intent('restart_playback')
        #     self.user_to_last_message = None

        # Stop download process if it's running.
        if self.curl:
            try:
                self.curl.kill()
                self.curl.communicate()
            except Exception as e:
                LOG.error('Could not stop curl: {}'.format(repr(e)))
            finally:
                self.curl = None
            # self.CPS_send_status()
            return True


def create_skill():
    return NewsSkill()
