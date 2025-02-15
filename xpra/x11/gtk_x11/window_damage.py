# This file is part of Xpra.
# Copyright (C) 2008, 2009 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2012-2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.


from xpra.util import envbool
from xpra.gtk_common.gobject_util import one_arg_signal
from xpra.x11.gtk_x11.gdk_bindings import (
            add_event_receiver,             #@UnresolvedImport
            remove_event_receiver,          #@UnresolvedImport
            )
from xpra.gtk_common.error import xsync, xlog, XError
from xpra.x11.common import Unmanageable

from xpra.x11.bindings.ximage import XImageBindings #@UnresolvedImport
from xpra.x11.bindings.window_bindings import constants, X11WindowBindings #@UnresolvedImport
from xpra.log import Logger

log = Logger("x11", "window", "damage")

XImage = XImageBindings()
X11Window = X11WindowBindings()
X11Window.ensure_XDamage_support()


StructureNotifyMask = constants["StructureNotifyMask"]
USE_XSHM = envbool("XPRA_XSHM", True)


class WindowDamageHandler:

    XShmEnabled = USE_XSHM
    MAX_RECEIVERS = 3

    __common_gsignals__ = {
                           "xpra-damage-event"     : one_arg_signal,
                           "xpra-unmap-event"      : one_arg_signal,
                           "xpra-configure-event"  : one_arg_signal,
                           "xpra-reparent-event"   : one_arg_signal,
                           }

    # This may raise XError.
    def __init__(self, client_window, use_xshm=USE_XSHM):
        self.client_window = client_window
        self.xid = client_window.get_xid()
        log("WindowDamageHandler.__init__(%#x, %s)", self.xid, use_xshm)
        self._use_xshm = use_xshm
        self._damage_handle = None
        self._xshm_handle = None
        self._contents_handle = None
        self._border_width = 0

    def __repr__(self):
        return "WindowDamageHandler(%#x)" % self.xid

    def setup(self):
        self.invalidate_pixmap()
        geom = X11Window.geometry_with_border(self.xid)
        if geom is None:
            raise Unmanageable("window %#x disappeared already" % self.xid)
        self._border_width = geom[-1]
        self.create_damage_handle()
        add_event_receiver(self.client_window, self, self.MAX_RECEIVERS)

    def create_damage_handle(self):
        self._damage_handle = X11Window.XDamageCreate(self.xid)
        log("damage handle(%#x)=%#x", self.xid, self._damage_handle)

    def destroy(self):
        if self.client_window is None:
            log.warn("damage window handler for %s already cleaned up!", self)
            return
        #clear the reference to the window early:
        win = self.client_window
        self.client_window = None
        self.do_destroy(win)

    def do_destroy(self, win):
        remove_event_receiver(win, self)
        self.destroy_damage_handle()

    def destroy_damage_handle(self):
        log("close_damage_handle()")
        self.invalidate_pixmap()
        dh = self._damage_handle
        if dh:
            self._damage_handle = None
            with xlog:
                X11Window.XDamageDestroy(dh)
        sh = self._xshm_handle
        if sh:
            self._xshm_handle = None
            with xlog:
                sh.cleanup()
        #note: this should be redundant since we cleared the
        #reference to self.client_window and shortcut out in do_get_property_contents_handle
        #but it's cheap anyway
        self.invalidate_pixmap()

    def acknowledge_changes(self):
        sh = self._xshm_handle
        dh = self._damage_handle
        log("acknowledge_changes() xshm handle=%s, damage handle=%s", sh, dh)
        if sh:
            sh.discard()
        if dh and self.client_window:
            #"Synchronously modifies the regions..." so unsynced?
            with xlog:
                X11Window.XDamageSubtract(dh)
            self.invalidate_pixmap()

    def invalidate_pixmap(self):
        ch = self._contents_handle
        log("invalidating named pixmap, contents handle=%s", ch)
        if ch:
            self._contents_handle = None
            with xlog:
                ch.cleanup()

    def has_xshm(self):
        return self._use_xshm and WindowDamageHandler.XShmEnabled and XImage.has_XShm()

    def get_xshm_handle(self):
        if not self.has_xshm():
            return None
        if self._xshm_handle:
            sw, sh = self._xshm_handle.get_size()
            ww, wh = self.client_window.get_geometry()[2:4]
            if sw!=ww or sh!=wh:
                #size has changed!
                #make sure the current wrapper gets garbage collected:
                self._xshm_handle.cleanup()
                self._xshm_handle = None
        if self._xshm_handle is None:
            #make a new one:
            self._xshm_handle = XImage.get_XShmWrapper(self.xid)
            if self._xshm_handle is None:
                #failed (may retry)
                return None
            init_ok, retry_window, xshm_failed = self._xshm_handle.setup()
            if not init_ok:
                #this handle is not valid, clear it:
                self._xshm_handle = None
            if not retry_window:
                #and it looks like it is not worth re-trying this window:
                self._use_xshm = False
            if xshm_failed:
                log.warn("Warning: disabling XShm support following irrecoverable error")
                WindowDamageHandler.XShmEnabled = False
        return self._xshm_handle

    def _set_pixmap(self):
        self._contents_handle = XImage.get_xwindow_pixmap_wrapper(self.xid)

    def get_contents_handle(self):
        if not self.client_window:
            #shortcut out
            return None
        if self._contents_handle is None:
            log("refreshing named pixmap")
            with xlog:
                self._set_pixmap()
        return self._contents_handle


    def get_image(self, x, y, width, height):
        handle = self.get_contents_handle()
        if handle is None:
            log("get_image(..) pixmap is None for window %#x", self.xid)
            return None

        #try XShm:
        try:
            with xsync:
                shm = self.get_xshm_handle()
                #log("get_image(..) XShm handle: %s, handle=%s, pixmap=%s", shm, handle, handle.get_pixmap())
                if shm is not None:
                    shm_image = shm.get_image(handle.get_pixmap(), x, y, width, height)
                    #log("get_image(..) XShm image: %s", shm_image)
                    if shm_image:
                        return shm_image
        except XError as e:
            if e.msg.startswith("BadMatch") or e.msg.startswith("BadWindow"):
                log("get_image(%s, %s, %s, %s) get_image BadMatch ignored (window already gone?)", x, y, width, height)
            else:
                log.warn("get_image(%s, %s, %s, %s) '%s'", x, y, width, height, e.msg, exc_info=True)

        try:
            w = min(handle.get_width(), width)
            h = min(handle.get_height(), height)
            if w!=width or h!=height:
                log("get_image(%s, %s, %s, %s) clamped to pixmap dimensions: %sx%s", x, y, width, height, w, h)
            with xsync:
                return handle.get_image(x, y, w, h)
        except XError as e:
            if e.msg.startswith("BadMatch"):
                log("get_image(%s, %s, %s, %s) get_image BadMatch ignored (window already gone?)", x, y, width, height)
            else:
                log.warn("Warning: cannot capture image of geometry %", (x, y, width, height), exc_info=True)
            return None


    def do_xpra_damage_event(self, _event):
        raise NotImplementedError()

    def do_xpra_reparent_event(self, _event):
        self.invalidate_pixmap()

    def xpra_unmap_event(self, _event):
        self.invalidate_pixmap()

    def do_xpra_configure_event(self, event):
        self._border_width = event.border_width
        self.invalidate_pixmap()
