#!/usr/bin/python


import platform
import xbmc
import xbmcaddon

import SimpleHTTPServer
import SocketServer
import urlparse

import threading

ADDON        = xbmcaddon.Addon()
ADDONVERSION = ADDON.getAddonInfo('version')
ADDONNAME    = ADDON.getAddonInfo('name')


class Main:
    def __init__(self):
        PORT = 19088
        httpd = SocketServer.TCPServer(("", PORT), WebHandler)         
        kodiWaiter = threading.Thread(target = self.kodiWaiterThread, args = (httpd, ))
        kodiWaiter.start()
        httpd.serve_forever()

    def kodiWaiterThread(self, httpd):
        monitor = xbmc.Monitor()
        while not monitor.abortRequested():
            if monitor.waitForAbort(3):
                 break
        xbmc.log('%s: Exiting' % (ADDONNAME))
        httpd.shutdown()


class WebHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
   def do_GET(self):
        # Parse query data & params to find out what was passed
        parsedParams = urlparse.urlparse(self.path)
        queryParsed = urlparse.parse_qs(parsedParams.query)
        
        xbmc.log('%s: Serve %s' % (ADDONNAME, parsedParams.path))
        
        if parsedParams.path.endswith(".avi"):
            url = "https://cdn-d4.soap4.me/20e42d6acbd06cba110198098d4f3c90806cda3c/1343/71d66d6fb822a4fca32bde89a8bdb408/"
            xbmc.log("%s: Redirect to '%s'" % (ADDONNAME, url))
            self.send_response(301)
            self.send_header('Location', url)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/html;charset=UTF-8')
        self.end_headers()
        
        self.wfile.write("<html>\n");
        self.wfile.write(" <head>\n");
        self.wfile.write("  <title>Index of /</title>\n");
        self.wfile.write(" </head>\n");
        self.wfile.write(" <body>\n");
        self.wfile.write("<h1>Index of /</h1>\n");
        self.wfile.write("  <table>\n");
        self.wfile.write("   <tr><th valign=\"top\"><img src=\"/icons/blank.gif\" alt=\"[ICO]\"></th><th><a href=\"?C=N;O=D\">Name</a></th><th><a href=\"?C=M;O=A\">Last modified</a></th><th><a href=\"?C=S;O=A\">Size</a></th><th><a href=\"?C=D;O=A\">Description</a></th></tr>\n");
        self.wfile.write("   <tr><th colspan=\"5\"><hr></th></tr>\n");
        self.wfile.write("   <tr><td valign=\"top\"><img src=\"/icons/back.gif\" alt=\"[PARENTDIR]\"></td><td><a href=\"/\">Parent Directory</a></td><td>&nbsp;</td><td align=\"right\">  - </td><td>&nbsp;</td></tr>\n");
        if parsedParams.path == '/':
            self.wfile.write("   <tr><td valign=\"top\"><img src=\"/icons/folder.gif\" alt=\"[DIR]\"></td><td><a href=\"The%20Big%20Bang%20Theory/\">The Big Bang Theory/</a></td><td align=\"right\">2016-11-01 23:18  </td><td align=\"right\">  - </td><td>&nbsp;</td></tr>\n");
        else:
            self.wfile.write("   <tr><td valign=\"top\"><img src=\"/icons/movie.gif\" alt=\"[VID]\"></td><td><a href=\"S05E02.avi\">S05E02.avi</a></td><td align=\"right\">2016-11-01 23:08  </td><td align=\"right\"> 0 </td><td>&nbsp;</td></tr>\n");
        self.wfile.write("   <tr><th colspan=\"5\"><hr></th></tr>\n");
        self.wfile.write("</table>\n");
        self.wfile.write("</body></html>\n");
        self.wfile.close();
        


if (__name__ == "__main__"):
    xbmc.log('%s: Version %s started' % (ADDONNAME, ADDONVERSION))
    Main()
