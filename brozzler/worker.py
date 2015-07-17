# vim: set sw=4 et:

import os
import logging
import brozzler
import threading
import time
import signal
import kombu
import brozzler.hq
import youtube_dl
import urllib.request
import json

class BrozzlerWorker:
    logger = logging.getLogger(__module__ + "." + __qualname__)

    def __init__(self, amqp_url="amqp://guest:guest@localhost:5672/%2f",
            max_browsers=1, chrome_exe="chromium-browser"):
        self._amqp_url = amqp_url
        self._max_browsers = max_browsers
        self._browser_pool = brozzler.browser.BrowserPool(max_browsers,
                chrome_exe=chrome_exe, ignore_cert_errors=True)
        self._shutdown_requested = threading.Event()

    def _youtube_dl(self, site):
        ydl_opts = {
            "outtmpl": "/dev/null",
            "verbose": False,
            "retries": 1,
            "logger": self.logger,
            "nocheckcertificate": True,
            "hls_prefer_native": True,
            "noprogress": True,
            "nopart": True,
            "no_color": True,
        }
        if site.proxy:
            ydl_opts["proxy"] = "http://{}".format(site.proxy)
            ## XXX (sometimes?) causes chrome debug websocket to go through
            ## proxy. Maybe not needed thanks to hls_prefer_native.
            ## # see https://github.com/rg3/youtube-dl/issues/6087
            ## os.environ["http_proxy"] = "http://{}".format(site.proxy)
        return youtube_dl.YoutubeDL(ydl_opts)

    def _next_page(self, site):
        """Raises kombu.simple.Empty if queue is empty"""
        with kombu.Connection(self._amqp_url) as conn:
            q = conn.SimpleQueue("brozzler.sites.{}.pages".format(site.id))
            msg = q.get(block=True, timeout=0.5)
            page_dict = msg.payload
            page = brozzler.Page(**page_dict)
            msg.ack()
            return page

    def _completed_page(self, site, page):
        with kombu.Connection(self._amqp_url) as conn:
            q = conn.SimpleQueue("brozzler.sites.{}.completed_pages".format(site.id))
            self.logger.info("putting {} on queue {}".format(page, q.queue.name))
            q.put(page.to_dict())

    def _disclaim_site(self, site, page=None):
        # XXX maybe should put on "disclaimed" queue and hq should put back on "unclaimed"
        with kombu.Connection(self._amqp_url) as conn:
            q = conn.SimpleQueue("brozzler.sites.unclaimed".format(site.id))
            self.logger.info("putting {} on queue {}".format(site, q.queue.name))
            q.put(site.to_dict())
            if page:
                q = conn.SimpleQueue("brozzler.sites.{}.pages".format(site.id))
                self.logger.info("putting unfinished page {} on queue {}".format(page, q.queue.name))
                q.put(page.to_dict())

    def _putmeta(self, warcprox_address, url, content_type, payload):
        request = urllib.request.Request(url, method="PUTMETA",
                headers={"Content-Type":content_type}, data=payload)

        # XXX setting request.type="http" is a hack to stop urllib from trying
        # to tunnel if url is https
        request.type = "http"
        request.set_proxy(warcprox_address, "http")

        try:
            with urllib.request.urlopen(request) as response:
                if response.status != 204:
                    self.logger.warn("""got "{} {}" response on warcprox PUTMETA request (expected 204)""".format(response.status, response.reason))
        except urllib.error.HTTPError as e:
            self.logger.warn("""got "{} {}" response on warcprox PUTMETA request (expected 204)""".format(e.getcode(), e.info()))

    def _try_youtube_dl(self, ydl, site, page):
        try:
            self.logger.info("trying youtube-dl on {}".format(page))
            info = ydl.extract_info(page.url)
            if site.proxy and site.enable_warcprox_features:
                info_json = json.dumps(info, sort_keys=True, indent=4)
                self.logger.info("sending PUTMETA request to warcprox with youtube-dl json for {}".format(page))
                self._putmeta(warcprox_address=site.proxy, url=page.url,
                        content_type="application/vnd.youtube-dl_formats+json;charset=utf-8",
                        payload=info_json.encode("utf-8"))
        except BaseException as e:
            if youtube_dl.utils.UnsupportedError in e.exc_info:
                pass
            else:
                raise

    def _brozzle_page(self, browser, ydl, site, page):
        def on_screenshot(screenshot_png):
            if site.proxy and site.enable_warcprox_features:
                self.logger.info("sending PUTMETA request to warcprox with screenshot for {}".format(page))
                self._putmeta(warcprox_address=site.proxy, url=page.url,
                        content_type="image/png", payload=screenshot_png)

        self.logger.info("brozzling {}".format(page))
        self._try_youtube_dl(ydl, site, page)

        page.outlinks = browser.browse_page(page.url,
                on_screenshot=on_screenshot,
                on_url_change=page.note_redirect)

    def _brozzle_site(self, browser, ydl, site):
        start = time.time()
        page = None
        try:
            browser.start(proxy=site.proxy)
            while not self._shutdown_requested.is_set() and time.time() - start < 60:
                try:
                    page = self._next_page(site)
                    self._brozzle_page(browser, ydl, site, page)
                    self._completed_page(site, page)
                    page = None
                except kombu.simple.Empty:
                    # if some timeout reached, re-raise?
                    pass
        # except kombu.simple.Empty:
        #     self.logger.info("finished {} (queue is empty)".format(site))
        except brozzler.browser.BrowsingAborted:
            self.logger.info("{} shut down".format(browser))
        finally:
            browser.stop()
            self._disclaim_site(site, page)
            self._browser_pool.release(browser)

    def run(self):
        latest_state = None
        while not self._shutdown_requested.is_set():
            with kombu.Connection(self._amqp_url) as conn:
                q = conn.SimpleQueue("brozzler.sites.unclaimed")
                q_empty = False
                if len(q) > 0:
                    try:
                        browser = self._browser_pool.acquire()
                        try:
                            msg = q.get(block=True, timeout=0.5)
                            site = brozzler.Site(**msg.payload)
                            msg.ack() # XXX ack only after browsing finished? kinda complicated
                            self.logger.info("browsing site {}".format(site))
                            ydl = self._youtube_dl(site)
                            th = threading.Thread(target=lambda: self._brozzle_site(browser, ydl, site),
                                    name="BrowsingThread-{}".format(site.scope_surt))
                            th.start()
                        except kombu.simple.Empty:
                            q_empty = True
                    except KeyError:
                        if latest_state != "browsers-busy":
                            self.logger.info("all {} browsers are busy".format(self._max_browsers))
                            latest_state = "browsers-busy"
                else:
                    q_empty = True

                if q_empty:
                    if latest_state != "no-unclaimed-sites":
                        self.logger.info("no unclaimed sites to browse")
                        latest_state = "no-unclaimed-sites"
            time.sleep(0.5)

    def start(self):
        th = threading.Thread(target=self.run, name="BrozzlerWorker")
        th.start()

    def shutdown_now(self):
        self.logger.info("brozzler worker shutting down")
        self._shutdown_requested.set()
        self._browser_pool.shutdown_now()

