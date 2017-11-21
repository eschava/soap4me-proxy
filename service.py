#!/usr/bin/python
# -*- coding: utf-8 -*-

import xbmc, xbmcgui, xbmcplugin, xbmcaddon

import os
import sys
import shutil
import urllib
import urllib2
import cookielib
import StringIO
import json
import gzip
import threading
import time
import hashlib
from itertools import ifilter
from traceback import format_exc

import SimpleHTTPServer
import SocketServer
import urlparse
import re

# produce web pages parseable by https://github.com/xbmc/xbmc/blob/master/xbmc/filesystem/HTTPDirectory.cpp

__addon__    = xbmcaddon.Addon()
ADDONVERSION = __addon__.getAddonInfo('version')
ADDONNAME    = __addon__.getAddonInfo('name')
ADDONID      = __addon__.getAddonInfo('id')
ADDONICON    = xbmc.translatePath(__addon__.getAddonInfo('icon'))
ADDONPROFILE = xbmc.translatePath(__addon__.getAddonInfo('profile'))


if getattr(xbmcgui.Dialog, 'notification', False):
    def message_ok(message):
        xbmcgui.Dialog().notification("Soap4.me", message, icon=xbmcgui.NOTIFICATION_INFO, sound=False)

    def message_error(message):
        xbmcgui.Dialog().notification("Soap4.me", message, icon=xbmcgui.NOTIFICATION_ERROR, sound=False)
else:
    def show_message(message):
        xbmc.executebuiltin('XBMC.Notification("%s", "%s", %s, "%s")'%("Soap4.me", message, 3000, ADDONICON))

    message_ok = show_message
    message_error = show_message


soappath = os.path.join(ADDONPROFILE, "soap4me-proxy")


class Main:
    def __init__(self):
        watched_status = WatchedStatus()

        api = SoapApi(watched_status)
        watched_status.soap_api = api
        api.main()

        try:
            httpd = SocketServer.TCPServer(("", KodiConfig.get_web_port()), WebHandler)
            httpd.api = api
            kodi_waiter = threading.Thread(target=self.kodi_waiter_thread, args=(httpd, watched_status,))
            kodi_waiter.start()
            httpd.serve_forever()
        except:
            message_error("Cannot create web-server, port is busy")
            xbmc.log('%s: %s' % (ADDONID, format_exc()), xbmc.LOGERROR)
            #raise


    @staticmethod
    def kodi_waiter_thread(httpd, watched_status):
        monitor = KodiMonitor(watched_status)
        while not monitor.abortRequested():
            if monitor.waitForAbort(3):
                 break
        xbmc.log('%s: Exiting' % (ADDONID))
        httpd.shutdown()


# noinspection PyPep8Naming
class KodiMonitor(xbmc.Monitor):
    def __init__(self, watched_status):
        self.watched_status = watched_status

    def onScanStarted(self, library):
        xbmc.Monitor.onScanStarted(self, library)
        xbmc.log('%s: Library scan \'%s\' started' % (ADDONID, library))

    def onScanFinished(self, library):
        xbmc.Monitor.onScanFinished(self, library)
        xbmc.log('%s: Library scan \'%s\' finished' % (ADDONID, library))
        self.watched_status.sync_status()  # TODO: do in new thread

    def onNotification(self, sender, method, data):
        xbmc.Monitor.onNotification(self, sender, method, data)
        xbmc.log('%s: Notification %s from %s, params: %s' % (ADDONID, method, sender, str(data)))

        if method == 'VideoLibrary.OnUpdate':
            params = json.loads(data)
            if 'item' in params and 'type' in params['item']:
                item_type = params['item']['type']
                if item_type == 'episode' and 'id' in params['item'] and 'playcount' in params:
                    item_id = params['item']['id']
                    playcount = params['playcount']
                    self.watched_status.update_server_status(item_id, playcount > 0)
        elif method == 'Player.OnStop':
            params = json.loads(data)
            if 'item' in params and 'type' in params['item']:
                item_type = params['item']['type']
                if item_type == 'episode' and 'id' in params['item']:
                    item_id = params['item']['id']
                    end = params['end']
                    if end:
                        self.watched_status.update_server_status(item_id, True)
                    else:
                        # resume time is not still updated so need to re-check time later
                        threading.Timer(3.0, self.onPlayerStopped, args=(item_id, )).start()

    def onPlayerStopped(self, item_id):
        episode_details = KodiApi.get_episode_details(item_id)
        position = episode_details['resume']['position']
        total = episode_details['resume']['total']
        if total > 0 and position / total > 0.9:
            self.watched_status.update_server_status(item_id, True)
        else:
            self.watched_status.update_server_position(item_id, position)


class WatchedStatus(object):
    watched_status = dict()
    show_position = dict()
    soap_api = None

    def set_server_status(self, imdb, season, episode, watched, position):
        # xbmc.log('%s: Watched status %s/%s/%s/%s' % (ADDONID, imdb, season, episode, watched))
        show_watched_status = self.watched_status.get(imdb)
        if show_watched_status is None:
            show_watched_status = dict()
            self.watched_status[imdb] = show_watched_status

        show_position = self.show_position.get(imdb)
        if show_position is None:
            show_position = dict()
            self.show_position[imdb] = show_position

        episode_key = season + '/' + episode
        show_watched_status[episode_key] = watched
        show_position[episode_key] = position

    def update_server_status(self, episode_id, watched):
        episode_details = KodiApi.get_episode_details(episode_id)
        show_id = episode_details['tvshowid']
        season = str(episode_details['season'])
        episode = str(episode_details['episode'])

        show = KodiApi.get_show_details(show_id)
        imdb = show['imdbnumber']

        show_watched_status = self.watched_status.get(imdb)
        if show_watched_status is None:
            show_watched_status = dict()
            self.watched_status[imdb] = show_watched_status

        episode_key = season + '/' + episode
        if show_watched_status.get(episode_key) != watched:
            eid = self.get_soap_episode_id(episode_details)
            if eid is not None:
                xbmc.log('%s: Updating remote watched status of show \'%s\' season %s episode %s to %s' % (ADDONID, imdb, season, episode, watched))
                sid = self.get_soap_season_id(episode_details)
                self.soap_api.mark_watched(sid, eid, watched)
                show_watched_status[episode_key] = watched

    def update_server_position(self, episode_id, position):
        if position >= 0:
            episode_details = KodiApi.get_episode_details(episode_id)
            eid = self.get_soap_episode_id(episode_details)

            if eid is not None:
                show_id = episode_details['tvshowid']
                season = str(episode_details['season'])
                episode = str(episode_details['episode'])

                show = KodiApi.get_show_details(show_id)
                imdb = show['imdbnumber']

                xbmc.log('%s: Updating position of show \'%s\' season %s episode %s to %s' % (ADDONID, imdb, season, episode, str(position)))
                sid = self.get_soap_season_id(episode_details)
                self.soap_api.set_position(sid, eid, position)

                show_position = self.show_position.get(imdb)
                if show_position is None:
                    show_position = dict()
                    self.show_position[imdb] = show_position
                episode_key = season + '/' + episode
                show_position[episode_key] = position

    def sync_status(self):
        for show in KodiApi.get_shows():
            imdb = show['imdbnumber']
            show_watched_status = self.watched_status.get(imdb)
            show_position = self.show_position.get(imdb)

            if show_watched_status is not None:
                show_id = show['tvshowid']
                for e in KodiApi.get_episodes(show_id):
                    season = str(e['season'])
                    episode = str(e['episode'])
                    kodi_watched = e['playcount'] > 0
                    episode_key = season + '/' + episode
                    watched = show_watched_status.get(episode_key)
                    if kodi_watched != watched:
                        xbmc.log('%s: Updating local watched status of show \'%s\' season %s episode %s to %s' % (ADDONID, imdb, season, episode, watched))
                        episode_id = e['episodeid']
                        KodiApi.set_watched(episode_id, watched)

                    kodi_position = e['resume']['position']
                    position = show_position.get(episode_key)
                    if position is not None and kodi_position != int(position):
                        xbmc.log('%s: Updating local position of show \'%s\' season %s episode %s from %s to %s' % (ADDONID, imdb, season, episode, kodi_position, position))
                        episode_id = e['episodeid']
                        KodiApi.set_position(episode_id, position)

    @staticmethod
    def get_soap_episode_id(episode_details):
        url = episode_details['file']
        return WebHandler.get_episode_id(url)

    @staticmethod
    def get_soap_season_id(episode_details):
        url = episode_details['file']
        return WebHandler.get_season_id(url)


class SoapCache(object):
    def __init__(self, path, lifetime=30):
        self.path = os.path.join(path, "cache")
        if not os.path.exists(self.path):
            os.makedirs(self.path)

        self.lifetime = lifetime

    def get(self, cache_id, use_lifetime=True):
        cache_id = filter(lambda c: c not in ",./", cache_id)
        filename = os.path.join(self.path, str(cache_id))
        if not os.path.exists(filename) or not os.path.isfile(filename):
            return False

        max_time = time.time() - self.lifetime * 60
        if use_lifetime and self and os.path.getmtime(filename) <= max_time:
            return False

        with open(filename, "r") as f:
            return f.read()

    def set(self, cache_id, text):
        cache_id = filter(lambda c: c not in ",./", cache_id)
        # if cache was removed
        if not os.path.exists(self.path):
            os.makedirs(self.path)
        filename = os.path.join(self.path, str(cache_id))
        with open(filename, "w") as f:
            f.write(text)

    def rm(self, cache_id):
        cache_id = filter(lambda c: c not in ",./", cache_id)
        filename = os.path.join(self.path, str(cache_id))
        try:
            os.remove(filename)
            return True
        except OSError:
            return False

    def rmall(self):
        shutil.rmtree(self.path)
        os.makedirs(self.path)


class SoapCookies(object):
    def __init__(self):
        self.CJ = cookielib.CookieJar()
        self._cookies = None
        self.path = soappath

    def _cookies_init(self):
        if self.CJ is None:
            return

        urllib2.install_opener(
            urllib2.build_opener(
                urllib2.HTTPCookieProcessor(self.CJ)
            )
        )

        self.cookie_path = os.path.join(self.path, 'cookies')
        if not os.path.exists(self.cookie_path):
            os.makedirs(self.cookie_path)
            # print '[%s]: os.makedirs(cookie_path=%s)' % (addon_id, cookie_path)

    def _cookies_load(self, req):
        if self.CJ is None:
            return

        cookie_send = {}
        for cookie_fname in os.listdir(self.cookie_path):
            cookie_file = os.path.join(self.cookie_path, cookie_fname)
            if os.path.isfile(cookie_file):
                cf = open(cookie_file, 'r')
                cookie_send[os.path.basename(cookie_file)] = cf.read()
                cf.close()
                # else: print '[%s]: NOT os.path.isfile(cookie_file=%s)' % (addon_id, cookie_file)

        cookie_string = urllib.urlencode(cookie_send).replace('&', '; ')
        req.add_header('Cookie', cookie_string)

    def _cookies_save(self):
        if self.CJ is None:
            return

        for Cook in self.CJ:
            cookie_file = os.path.join(self.cookie_path, Cook.name)
            cf = open(cookie_file, 'w')
            cf.write(Cook.value)
            cf.close()


class SoapHttpClient(SoapCookies):
    HOST = 'https://api.soap4.me/v2'

    def __init__(self):
        self.token = None
        self.cache = SoapCache(soappath, 5)
        SoapCookies.__init__(self)

    def set_token(self, token):
        self.token = token

    def _post_data(self, params=None):
        if not isinstance(params, dict):
            return None

        return urllib.urlencode(params)

    def _request(self, url, params=None):
        xbmc.log('{0}: REQUEST: {1} {2}'.format(ADDONID, url, params))
        self._cookies_init()

        req = urllib2.Request(self.HOST + url)
        req.add_header('User-Agent', 'Kodi: plugin.soap4me-proxy v{0}'.format(ADDONVERSION))
        req.add_header('Accept-encoding', 'gzip')
        req.add_header('Kodi-Debug', '{0}'.format(xbmc.getInfoLabel('System.BuildVersion')))

        if self.token is not None:
            self._cookies_load(req)
            req.add_header('X-API-TOKEN', self.token)

        post_data = self._post_data(params)
        if params is not None:
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        response = urllib2.urlopen(req, post_data)

        self._cookies_save()

        text = None
        if response.info().get('Content-Encoding') == 'gzip':
            buffer = StringIO.StringIO(response.read())
            fstream = gzip.GzipFile(fileobj=buffer)
            text = fstream.read()
        else:
            text = response.read()
            response.close()

        return text

    def request(self, url, params=None, use_cache=False):
        text = None
        if use_cache:
            text = self.cache.get(url)

        if text is None or not text:
            text = self._request(url, params)

            if use_cache:
                self.cache.set(url, text)
        else:
            xbmc.log('%s: Url \'%s\' present in cache' % (ADDONID, url))

        try:
            return json.loads(text)
        except:
            return text

    def clean(self, url):
        if self.cache.rm(url):
            xbmc.log('%s: Url \'%s\' removed from cache' % (ADDONID, url))

    def clean_all(self):
        self.cache.rmall()


to_int = lambda s: int(s) if s != '' else 0


class KodiConfig(object):
    @classmethod
    def soap_get_auth(cls):
        return {
            'token': __addon__.getSetting('_token'),
            'token_sid': __addon__.getSetting('_token_sid'),
            'token_till': to_int(__addon__.getSetting('_token_till')),
            'token_valid': to_int(__addon__.getSetting('_token_valid')),
            'token_check': to_int(__addon__.getSetting('_token_check')),
            'message_till_days': to_int(__addon__.getSetting('_message_till_days'))
        }

    @classmethod
    def soap_set_auth(cls, params):
        __addon__.setSetting('_token', params.get('token', ''))
        __addon__.setSetting('_token_till', str(params.get('till', 0)))
        __addon__.setSetting('_token_sid', str(params.get('sid', '')))
        __addon__.setSetting('_message_till_days', '')
        cls.soap_set_token_valid()

    @classmethod
    def soap_set_token_valid(cls):
        __addon__.setSetting('_token_valid', str(int(time.time()) + 86400 * 7))

    @classmethod
    def soap_set_token_check(cls):
        __addon__.setSetting('_token_check', str(int(time.time()) + 600))

    @classmethod
    def message_till_days(cls):
        mtd = __addon__.getSetting('_message_till_days')
        if mtd == '' or int(mtd) < time.time():
            __addon__.setSetting('_message_till_days', str(int(time.time()) + 43200))
            till = to_int(__addon__.getSetting('_token_till'))
            if till != 0:
                message_ok("Осталось {0} дней".format(int(till - time.time()) / 86400))

    @classmethod
    def kodi_get_auth(cls):
        username = __addon__.getSetting('username')
        password = __addon__.getSetting('password')

        while len(username) == 0 or len(password) == 0:
            __addon__.openSettings()
            username = __addon__.getSetting('username')
            password = __addon__.getSetting('password')

        return {
            'login': username,
            'password': password
        }

    @classmethod
    def get_web_port(cls):
        return to_int(__addon__.getSetting('port'))

    @classmethod
    def is_hide_watched_shows(cls):
        return __addon__.getSetting('hide_watched_shows') == 'true'


class SoapConfig(object):
    def __init__(self):
        self.language = to_int(__addon__.getSetting('language'))  # 0 rus, 1 orig
        self.subtitles_language = to_int(__addon__.getSetting('subtitles_language'))  # 0 rus, 1 orig
        self.quality = to_int(__addon__.getSetting('quality'))  # 0 SD, 1 720p, 2 FullHD, 3 2K, 4 4K


class SoapAuth(object):
    AUTH_URL = '/auth/'
    CHECK_URL = '/auth/check/'

    def __init__(self, client):
        self.client = client
        self.is_auth = False

    def login(self):
        self.client.set_token(None)
        data = self.client.request(self.AUTH_URL, KodiConfig.kodi_get_auth())

        if not isinstance(data, dict) or data.get('ok') != 1:
            message_error("Login or password are incorrect")
            return False

        KodiConfig.soap_set_auth(data)
        return True

    def check(self):
        params = KodiConfig.soap_get_auth()

        if params['token'] == '':
            return False

        if params['token_valid'] < time.time():
            return False

        if params['token_till'] + 10 < time.time():
            return False

        self.client.set_token(params['token'])

        if params['token_check'] > time.time():
            return True

        data = self.client.request(self.CHECK_URL)
        if isinstance(data, dict) and data.get('loged') == 1:
            KodiConfig.soap_set_token_check()
            return True

        return False

    def auth(self):
        if not self.check():
            if not self.login():
                return False

        params = KodiConfig.soap_get_auth()
        if not params['token']:
            message_error("Auth error")
            return False

        self.client.set_token(params['token'])
        self.is_auth = True


class SoapApi(object):
    MY_SHOWS_URL = '/soap/my/'
    EPISODES_URL = '/episodes/{0}/'

    # WATCHING_URL = {
    #     'watch': '/soap/watch/{sid}/',
    #     'unwatch': '/soap/unwatch/{sid}/'
    # }
    #
    PLAY_EPISODES_URL = '/play/episode/{eid}/'
    SAVE_POSITION_URL = '/play/episode/{eid}/savets/'
    MARK_WATCHED = '/episodes/watch/{eid}/'
    MARK_UNWATCHED = '/episodes/unwatch/{eid}/'

    def __init__(self, watched_status):
        self.client = SoapHttpClient()
        self.auth = SoapAuth(self.client)
        self.config = SoapConfig()
        self.watched_status = watched_status

        self.auth.auth()

    @property
    def is_auth(self):
        return self.auth.is_auth

    def main(self):
        KodiConfig.message_till_days()

    def my_shows(self, hide_watched=False):
        data = self.client.request(self.MY_SHOWS_URL, use_cache=True)
        if hide_watched:
            data = filter(lambda row: row['unwatched'] > 0, data)
        # TODO: tvdb_id is used as IMDB because Kodi uses TVDB internally for imdbnumber key
        return map(lambda row: {'name': row['title'], 'id': row['sid'], 'IMDB': row['tvdb_id'].replace('tt', '')}, data)

    def episodes(self, sid, imdb):
        data = self.client.request(self.EPISODES_URL.format(sid), use_cache=True)
        data = data['episodes']

        if data is None:
            return []

        for e in data:
            self.watched_status.set_server_status(imdb, e['season'], e['episode'], e['watched'] == 1, e['start_from'])

        return map(lambda row: self.get_episode(row), data)

    def get_episode(self, row):
        f = self.get_best_file(row['files'])
        return {'season': row['season'], 'episode': row['episode'], 'id': f['eid'], 'hash': f['hash']}

    def get_best_file(self, files):
        return max(files, key=self.get_file_order)

    def get_file_order(self, f):
        translate = int(f['translate']) - 1  # from 0
        quality = int(f['quality']) - 1  # from 0

        translate_matrix = \
            [
                [-4, -3, -1,  0],  # eng
                [-1, -1, -3, -2],  # rus with eng subs
                [-2, -3, -1, -1],  # eng with rus subs
                [ 0, -1, -3, -4],  # rus
            ]

        config_translate_index = 2 * self.config.language + self.config.subtitles_language
        translate_order = translate_matrix[translate][config_translate_index]
        quality_order = \
            (quality - self.config.quality) \
            if (quality <= self.config.quality) \
            else (self.config.quality - quality - 10)
        return 100 * translate_order + quality_order  # translation has priority over quality

    def get_episode_url(self, sid, eid, ehash):
        # TODO: warn if quality is bigger than configured
        myhash = hashlib.md5(
                    str(self.client.token) +
                    str(eid) +
                    str(sid) +
                    str(ehash)
                ).hexdigest()

        data = {
            "eid": eid,
            "hash": myhash
        }
        result = self.client.request(self.PLAY_EPISODES_URL.format(eid=eid), data)
        return result['stream']

    def mark_watched(self, sid, eid, watched):
        url = self.MARK_WATCHED if watched else self.MARK_UNWATCHED
        self.client.request(url.format(eid=eid), {'eid': eid})
        # clean cache for show
        url = self.EPISODES_URL.format(sid)
        self.client.clean(url)

    def set_position(self, sid, eid, position):
        url = self.SAVE_POSITION_URL
        self.client.request(url.format(eid=eid), {'eid': eid, 'time': position})
        # clean cache for show
        url = self.EPISODES_URL.format(sid)
        self.client.clean(url)


class KodiApi(object):
    @staticmethod
    def get_shows():
        postdata = json.dumps({"jsonrpc": "2.0",
                               "id": 1,
                               'method': 'VideoLibrary.GetTVShows',
                               "params": {
                                   "properties": ["imdbnumber"]
                               }})
        json_query = xbmc.executeJSONRPC(postdata)
        json_query = unicode(json_query, 'utf-8', errors='ignore')
        json_query = json.loads(json_query)['result']['tvshows']
        return json_query

    @staticmethod
    def get_show_details(show_id):
        postdata = json.dumps({"jsonrpc": "2.0",
                               "id": 1,
                               'method': 'VideoLibrary.GetTVShowDetails',
                               "params": {
                                   'tvshowid': show_id,
                                   "properties": ["imdbnumber"]
                               }})
        json_query = xbmc.executeJSONRPC(postdata)
        json_query = unicode(json_query, 'utf-8', errors='ignore')
        json_query = json.loads(json_query)['result']['tvshowdetails']
        return json_query

    @staticmethod
    def get_episodes(show_id):
        postdata = json.dumps({"jsonrpc": "2.0",
                               "id": 1,
                               'method': 'VideoLibrary.GetEpisodes',
                               "params": {
                                   'tvshowid': show_id,
                                   "properties": ["season", "episode", "playcount", "resume"]
                               }})
        json_query = xbmc.executeJSONRPC(postdata)
        json_query = json.loads(json_query)
        if 'error' in json_query:
            xbmc.log('%s: ERROR: %s' % (ADDONID, json_query['error']['stack']['message']))
            return None
        json_query = json_query['result']['episodes']
        return json_query

    @staticmethod
    def get_episode_details(episode_id):
        postdata = json.dumps({"jsonrpc": "2.0",
                               "id": 1,
                               'method': 'VideoLibrary.GetEpisodeDetails',
                               "params": {
                                   'episodeid': episode_id,
                                   "properties": ["season", "episode", "tvshowid", "playcount", "file", "resume"]
                               }})
        json_query = xbmc.executeJSONRPC(postdata)
        json_query = unicode(json_query, 'utf-8', errors='ignore')
        json_query = json.loads(json_query)
        if 'error' in json_query:
            xbmc.log('%s: ERROR: %s' % (ADDONID, json_query['error']['stack']['message']))
            return None
        json_query = json_query['result']['episodedetails']
        return json_query

    @staticmethod
    def set_watched(episode_id, watched):
        postdata = json.dumps({"jsonrpc": "2.0",
                               "id": 1,
                               'method': 'VideoLibrary.SetEpisodeDetails',
                               "params": {
                                   'episodeid': episode_id,
                                   'playcount': 1 if watched else 0,
                               }})
        xbmc.executeJSONRPC(postdata)

    @staticmethod
    def set_position(episode_id, position):
        postdata = json.dumps({"jsonrpc": "2.0",
                               "id": 1,
                               'method': 'VideoLibrary.SetEpisodeDetails',
                               "params": {
                                   'episodeid': episode_id,
                                   'resume': {'position': int(position)},
                               }})
        xbmc.executeJSONRPC(postdata)


# NOTE: standard ?param=value&param2=value2... notation is not used for url parameters because of
# issue with endless directory scanning by Kodi
# so for folder is used only show name
# and for file name custom prefix containing all required IDs is used
class WebHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
    match = None

    def do_GET(self):
        # Parse path to find out what was passed
        xbmc.log('%s: Serve \'%s\'' % (ADDONID, self.path))
        parsed_params = urlparse.urlparse(self.path)
        path = urllib.unquote(parsed_params.path)

        if path == '/':
            xbmc.log('%s: Listing shows' % ADDONID)
            shows = self.server.api.my_shows(KodiConfig.is_hide_watched_shows())

            self.out_folders(map(lambda s: s['name'], shows))
        elif self.matches('^/(.*)/$', path):
            show = self.match.group(1)
            show_details = self.find_show(show)
            if show_details is not None:
                sid = show_details['id']
                imdb = show_details['IMDB']

                xbmc.log('%s: Listing episodes of \'%s\'' % (ADDONID, show))
                episodes = self.server.api.episodes(sid, imdb)

                # format parsable by TVDB scraper
                name_lambda = lambda e: sid + '_' + e['id'] + '_' + e['hash'] + '_S' + e['season'] + 'E' + e['episode'] + '.avi'
                self.out_files(map(name_lambda, episodes))
            else:
                xbmc.log('%s: ERROR: Show \'%s\' not found' % (ADDONID, show))
        elif self.matches('^/(.*)/(\d+)_(\d+)_([0-9a-f]+)_S(\d+)E(\d+).avi$', path):
            show = self.match.group(1)
            sid = self.match.group(2)
            eid = self.match.group(3)
            ehash = self.match.group(4)
            season = self.match.group(5)
            episode = self.match.group(6)

            xbmc.log('%s: Requested episode %s from season %s of \'%s\'' % (ADDONID, episode, season, show))
            url = self.server.api.get_episode_url(sid, eid, ehash)

            xbmc.log("%s: Redirect to '%s'" % (ADDONID, url))
            self.send_response(301)
            self.send_header('Location', url)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        # Parse path to find out what was passed
        xbmc.log('%s: Head \'%s\'' % (ADDONID, self.path))
        parsed_params = urlparse.urlparse(self.path)
        path = urllib.unquote(parsed_params.path)

        if path == '/':
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
        elif self.matches('^/(.*)/$', path):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
        elif self.matches('^/(.*)/(\d+)_(\d+)_([0-9a-f]+)_S(\d+)E(\d+).avi$', path):
            self.send_response(200)
            self.send_header("Content-type", "video/mp4")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def matches(self, regexp, s):
        self.match = re.match(regexp, s, re.M | re.I)
        return self.match is not None

    @staticmethod
    def get_episode_id(url):
        parsed_params = urlparse.urlparse(url)
        file_name = os.path.basename(parsed_params.path)
        return file_name.split('_')[1]

    @staticmethod
    def get_season_id(url):
        parsed_params = urlparse.urlparse(url)
        file_name = os.path.basename(parsed_params.path)
        return file_name.split('_')[0]

    def out_folders(self, folders):
        self.out_elements(map(lambda f: "<tr>"
                                        "  <td valign=\"top\"><img src=\"/icons/folder.gif\" alt=\"[DIR]\"></td>"
                                        "  <td><a href=\"%s/\">%s</a></td>"
                                        "  <td align=\"right\">2016-11-01 23:18</td>"
                                        "  <td align=\"right\">  - </td>"
                                        "  <td>&nbsp;</td>"
                                        "</tr>\n" % (urllib.quote(f), f), folders))

    def out_files(self, files):
        self.out_elements(map(lambda f: "<tr> "
                                        "  <td valign=\"top\"><img src=\"/icons/movie.gif\" alt=\"[VID]\"></td>"
                                        "  <td><a href=\"%s\">%s</a></td>"
                                        "  <td align=\"right\">2016-11-01 23:08</td>"
                                        "  <td align=\"right\"> 0 </td>"
                                        "  <td>&nbsp;</td>"
                                        "</tr>\n" % (f, f), files))

    def out_elements(self, elements):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html;charset=UTF-8')
        self.end_headers()

        self.wfile.write("<html>\n")
        self.wfile.write(" <head>\n")
        self.wfile.write("  <title>Index of /</title>\n")
        self.wfile.write(" </head>\n")
        self.wfile.write(" <body>\n")
        self.wfile.write("<h1>Index of /</h1>\n")
        self.wfile.write("  <table>\n")
        self.wfile.write("   <tr><th valign=\"top\"><img src=\"/icons/blank.gif\" alt=\"[ICO]\"></th><th><a href=\"?C=N;O=D\">Name</a></th><th><a href=\"?C=M;O=A\">Last modified</a></th><th><a href=\"?C=S;O=A\">Size</a></th><th><a href=\"?C=D;O=A\">Description</a></th></tr>\n")
        self.wfile.write("   <tr><th colspan=\"5\"><hr></th></tr>\n")
        self.wfile.write("   <tr><td valign=\"top\"><img src=\"/icons/back.gif\" alt=\"[PARENTDIR]\"></td><td><a href=\"/\">Parent Directory</a></td><td>&nbsp;</td><td align=\"right\">  - </td><td>&nbsp;</td></tr>\n")
        for e in elements:
            self.wfile.write(e)
        self.wfile.write("   <tr><th colspan=\"5\"><hr></th></tr>\n")
        self.wfile.write("</table>\n")
        self.wfile.write("</body></html>\n")
        self.wfile.close()

    def find_show(self, show):
        shows = self.server.api.my_shows()  # should be cached
        return next(ifilter(lambda s: show == s['name'], shows), None)

    # next methods were added to minimize number of messages printed to log
    # because Kodi closes socket connection on error code
    def handle_one_request(self):
        try:
            SimpleHTTPServer.SimpleHTTPRequestHandler.handle_one_request(self)
        except IOError:
            pass  # it's OK

    def finish(self):
        try:
            SimpleHTTPServer.SimpleHTTPRequestHandler.finish(self)  # super.finish()
        except IOError:
            pass  # it's OK

    def log_request(self, code='-', size='-'):
        pass  # already logged


def clean_cache():
    SoapCache(soappath, 5).rmall()
    __addon__.setSetting('_token', '0')
    __addon__.setSetting('_token_sid', '0')
    __addon__.setSetting('_token_valid', '0')
    __addon__.setSetting('_token_till', '0')
    __addon__.setSetting('_token_check', '0')
    __addon__.setSetting('_message_till_days', '0')


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == 'clearcache':
        clean_cache()
        message_ok('Done')
        exit(0)

    xbmc.log('%s: Version %s started' % (ADDONID, ADDONVERSION))
    Main()
