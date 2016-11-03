#!/usr/bin/python
# -*- coding: utf-8 -*-

import xbmc, xbmcgui, xbmcplugin, xbmcaddon

import os
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
        api = SoapApi()
        api.main()

        httpd = SocketServer.TCPServer(("", KodiConfig.get_web_port()), WebHandler)
        httpd.api = api
        kodi_waiter = threading.Thread(target=self.kodi_waiter_thread, args=(httpd,))
        kodi_waiter.start()
        httpd.serve_forever()

    @staticmethod
    def kodi_waiter_thread(httpd):
        monitor = xbmc.Monitor()
        while not monitor.abortRequested():
            if monitor.waitForAbort(3):
                 break
        xbmc.log('%s: Exiting' % (ADDONNAME))
        httpd.shutdown()


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
        filename = os.path.join(self.path, str(cache_id))
        with open(filename, "w") as f:
            f.write(text)

    def rm(self, cache_id):
        cache_id = filter(lambda c: c not in ",./", cache_id)
        filename = os.path.join(self.path, str(cache_id))
        os.remove(filename)

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
        self.cache.rm(url)

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
    # SAVE_POSITION_URL = '/play/episode/{eid}/savets/'
    # MARK_WATCHED = '/episodes/watch/{eid}/'
    # MARK_UNWATCHED = '/episodes/unwatch/{eid}/'

    def __init__(self):
        self.client = SoapHttpClient()
        self.auth = SoapAuth(self.client)
        self.config = SoapConfig()

        self.auth.auth()

    @property
    def is_auth(self):
        return self.auth.is_auth

    def main(self):
        KodiConfig.message_till_days()

    def my_shows(self):
        data = self.client.request(self.MY_SHOWS_URL, use_cache=True)
        return map(lambda row: {'name': row['title'], 'id': row['sid']}, data)

    def episodes(self, sid):
        data = self.client.request(self.EPISODES_URL.format(sid), use_cache=True)
        data = data['episodes']
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

        config_translate_index = 2 * self.config.language * 2 + self.config.subtitles_language
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


class WebHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
    match = None

    def do_GET(self):
        # Parse query data & params to find out what was passed
        xbmc.log('%s: Serve \'%s\'' % (ADDONID, self.path))
        parsed_params = urlparse.urlparse(self.path)
        query_parsed = urlparse.parse_qs(parsed_params.query)
        path = urllib.unquote(parsed_params.path)

        if path == '/':
            xbmc.log('%s: Listing shows' % ADDONID)
            shows = self.server.api.my_shows()

            self.out_folders(shows,
                             lambda s: s['name'] + '/',
                             lambda s: urllib.quote(s['name']) + '/?id=' + s['id'])
        elif self.matches('^/(.*)/$', path):
            show = self.match.group(1)
            sid = query_parsed['id'][0]

            xbmc.log('%s: Listing episodes of \'%s\'' % (ADDONID, show))
            episodes = self.server.api.episodes(sid)

            self.out_files(episodes,
                             lambda e: 'S' + e['season'] + 'E' + e['episode'] + '.avi',
                             lambda e: 'S' + e['season'] + 'E' + e['episode'] + '.avi?sid=' + sid + '&amp;id=' + e['id'] + '&amp;hash=' + e['hash'])
        elif self.matches('^/(.*)/S(\d+)E(\d+).avi$', path):
            show = self.match.group(1)
            season = self.match.group(2)
            episode = self.match.group(3)
            sid = query_parsed['sid'][0]
            eid = query_parsed['id'][0]
            ehash = query_parsed['hash'][0]

            xbmc.log('%s: Requested episode %s from season %s of \'%s\'' % (ADDONID, episode, season, show))
            url = self.server.api.get_episode_url(sid, eid, ehash)

            xbmc.log("%s: Redirect to '%s'" % (ADDONID, url))
            self.send_response(301)
            self.send_header('Location', url)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
            return

    def matches(self, regexp, s):
        self.match = re.match(regexp, s, re.M | re.I)
        return self.match is not None

    def out_folders(self, folders, name_lambda, url_lambda):
        self.out_elements(map(lambda f: "   <tr><td valign=\"top\"><img src=\"/icons/folder.gif\" alt=\"[DIR]\"></td><td><a href=\"%s\">%s</a></td><td align=\"right\">2016-11-01 23:18</td><td align=\"right\">  - </td><td>&nbsp;</td></tr>\n" % (url_lambda(f), name_lambda(f)), folders))

    def out_files(self, files, name_lambda, url_lambda):
        self.out_elements(map(lambda f: "   <tr><td valign=\"top\"><img src=\"/icons/movie.gif\" alt=\"[VID]\"></td><td><a href=\"%s\">%s</a></td><td align=\"right\">2016-11-01 23:08</td><td align=\"right\"> 0 </td><td>&nbsp;</td></tr>\n" % (url_lambda(f), name_lambda(f)), files))

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


if __name__ == "__main__":
    xbmc.log('%s: Version %s started' % (ADDONID, ADDONVERSION))
    Main()
