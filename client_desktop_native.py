"""Access the original Windows Explorer desktop items and their icon view."""

import ctypes as C
from ctypes import wintypes as W
import os
import struct
from pathlib import PureWindowsPath
from subprocess import list2cmdline
import uuid


PTR = C.c_void_p
DWORD = W.DWORD
HRESULT = C.c_long
P_PTR = C.POINTER(PTR)
POINT = W.POINT
FWF_AUTOARRANGE = 0x1
FWF_SNAPTOGRID = 0x4
FWF_NOICONS = 0x1000
LAYOUT_FLAGS = FWF_AUTOARRANGE | FWF_SNAPTOGRID
FVO_VISTALAYOUT = 0x1
FVO_CUSTOMPOSITION = 0x2
E_NOTIMPL = 0x80004001
SVSI_POSITIONITEM = 0x80
SVGIO_ALLVIEW = 2
SHELL_ID_LIST_MIME = 'application/x-qt-windows-mime;value="Shell IDList Array"'


class ShellFileInfo(C.Structure):
    _fields_ = [('icon', W.HICON), ('index', C.c_int), ('attributes', DWORD),
                ('name', W.WCHAR * 260), ('type_name', W.WCHAR * 80)]


class StockIconInfo(C.Structure):
    _fields_ = [('size', DWORD), ('icon', W.HICON), ('system_index', C.c_int),
                ('icon_index', C.c_int), ('path', W.WCHAR * 260)]


class ShellExecuteInfo(C.Structure):
    _fields_ = [('size', DWORD), ('mask', C.c_ulong), ('window', W.HWND),
                ('verb', W.LPCWSTR), ('file', W.LPCWSTR), ('parameters', W.LPCWSTR),
                ('directory', W.LPCWSTR), ('show', C.c_int), ('instance', W.HINSTANCE),
                ('id_list', PTR), ('file_class', W.LPCWSTR), ('class_key', W.HKEY),
                ('hot_key', DWORD), ('monitor', W.HANDLE), ('process', W.HANDLE)]


class InvokeCommandInfo(C.Structure):
    _fields_ = [('size', DWORD), ('mask', DWORD), ('owner', W.HWND), ('verb', PTR),
                ('parameters', PTR), ('directory', PTR), ('show', C.c_int),
                ('hot_key', DWORD), ('icon', W.HANDLE)]


def shell_image_list(shell, pixel_limit):
    """Use the largest original raster that fits, with the smallest list as a fallback."""
    iid = GUID('46eb5926-582e-4017-9fdf-e8998daa0950')
    for kind in (4, 2, 0, 1):  # Jumbo, extra large, large, small.
        images = PTR()
        if shell.SHGetImageList(kind, C.byref(iid), C.byref(images)) < 0 or not images:
            continue
        width, height = C.c_int(), C.c_int()
        result = call(images, 16, HRESULT, (PTR, PTR), C.byref(width), C.byref(height))
        if result >= 0 and (max(width.value, height.value) <= pixel_limit or kind == 1):
            return images
        call(images, 2, W.ULONG, ())
    return None


def image_list_icon(images, user, index, overlay=0):
    from PySide6.QtGui import QImage

    icon = PTR()
    call(images, 10, HRESULT, (C.c_int, C.c_uint, P_PTR), index, 1 | (overlay << 8), C.byref(icon))
    if not icon:
        return QImage()
    try:
        return QImage.fromHICON(icon.value)
    finally:
        user.DestroyIcon(icon)


def small_shortcut_arrow(images, user, overlay, shortcut_index):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QImage

    index = C.c_int()
    if (call(images, 31, HRESULT, (C.c_int, PTR), overlay, C.byref(index)) < 0
            or index.value != shortcut_index):
        return None
    arrow = image_list_icon(images, user, index.value).convertToFormat(QImage.Format.Format_RGBA8888)
    if arrow.isNull():
        return None
    pixels = arrow.constBits()
    left, right, top, bottom = arrow.width(), -1, arrow.height(), -1
    for y in range(arrow.height()):
        for x, alpha in enumerate(pixels[y * arrow.bytesPerLine() + 3:(y + 1) * arrow.bytesPerLine():4]):
            if alpha:
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
    if right < left or bottom < top:
        return None
    width, height = right - left + 1, bottom - top + 1
    resized = arrow.copy(left, top, width, height).scaled(
        max(1, round(width * .6)), max(1, round(height * .6)),
        Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return QPoint(left, bottom + 1 - resized.height()), resized


def desktop_icon_details(keys, pixel_limit):
    """Read the Shell's existing display names and DPI-appropriate artwork off the UI thread."""
    from PySide6.QtGui import QImage, QPainter

    ole = C.WinDLL('ole32')
    check(ole.CoInitializeEx(None, 2), 'CoInitializeEx')
    images = None
    try:
        shell = C.WinDLL('shell32')
        user = C.WinDLL('user32')
        ole.CoTaskMemFree.argtypes = (PTR,)
        user.DestroyIcon.argtypes = (W.HICON,)
        shell.SHParseDisplayName.argtypes = (W.LPCWSTR, PTR, P_PTR, DWORD, PTR)
        shell.SHParseDisplayName.restype = HRESULT
        shell.SHGetFileInfoW.argtypes = (PTR, DWORD, C.POINTER(ShellFileInfo), C.c_uint, C.c_uint)
        shell.SHGetFileInfoW.restype = C.c_size_t
        shell.SHGetImageList.argtypes = (C.c_int, PTR, P_PTR)
        shell.SHGetImageList.restype = HRESULT
        shell.SHGetStockIconInfo.argtypes = (C.c_int, C.c_uint, C.POINTER(StockIconInfo))
        shell.SHGetStockIconInfo.restype = HRESULT
        images = shell_image_list(shell, pixel_limit)
        shortcut = StockIconInfo(size=C.sizeof(StockIconInfo))
        shortcut_index = shortcut.system_index if shell.SHGetStockIconInfo(
            29, 0x4000, C.byref(shortcut)) >= 0 else -1  # SIID_LINK, SHGSI_SYSICONINDEX.
        arrows = {}
        for key in keys:
            info = ShellFileInfo()
            pidl = PTR()
            image = QImage()
            try:
                if shell.SHParseDisplayName(key, None, C.byref(pidl), 0, None) >= 0 and pidl:
                    flags = 0x8 | 0x100 | 0x200 | 0x20  # PIDL, icon, display name, apply overlays.
                    if images:
                        flags |= 0x4000 | 0x40  # System image index, overlay index.
                    if shell.SHGetFileInfoW(pidl, 0, C.byref(info), C.sizeof(info), flags) and images:
                        index = info.index & 0xffffff
                        overlay = (info.index >> 24) & 0xf
                        if overlay and overlay not in arrows:
                            arrows[overlay] = small_shortcut_arrow(images, user, overlay, shortcut_index)
                        arrow = arrows.get(overlay)
                        image = image_list_icon(images, user, index, 0 if arrow else overlay)
                        if arrow and not image.isNull():
                            image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                            painter = QPainter(image)
                            painter.drawImage(*arrow)
                            painter.end()
                if image.isNull() and info.icon:
                    image = QImage.fromHICON(info.icon)
            finally:
                if pidl:
                    ole.CoTaskMemFree(pidl)
                if info.icon:
                    user.DestroyIcon(info.icon)
            yield key, info.name or PureWindowsPath(key).name, image
    finally:
        if images:
            call(images, 2, W.ULONG, ())
        ole.CoUninitialize()


class GUID(C.Structure):
    _fields_ = [('value', C.c_ubyte * 16)]

    def __init__(self, text):
        super().__init__((C.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le))


class VariantData(C.Union):
    # Two pointer-sized fields are required for VT_RECORD, including on x64.
    _fields_ = [('integer', C.c_long), ('record', PTR * 2)]


class Variant(C.Structure):
    _fields_ = [('kind', W.WORD), ('reserved', W.WORD * 3), ('data', VariantData)]


def check(result, operation):
    if result < 0:
        raise OSError(f'{operation} (0x{result & 0xffffffff:08X})')


def call(interface, index, result, types, *args):
    address = C.cast(interface, C.POINTER(C.POINTER(PTR))).contents[index]
    return C.WINFUNCTYPE(result, PTR, *types)(address)(interface, *args)


class ShellDrop:
    def __init__(self, target, data):
        self.target = target
        self.data = data
        self.entered = False

    def update(self, keys, point, actions):
        effect = DWORD(actions)
        if self.entered:
            check(call(self.target, 4, HRESULT, (DWORD, POINT, PTR), keys, point, C.byref(effect)),
                  'IDropTarget.DragOver')
        else:
            check(call(self.target, 3, HRESULT, (PTR, DWORD, POINT, PTR),
                       self.data, keys, point, C.byref(effect)), 'IDropTarget.DragEnter')
            self.entered = True
        return effect.value & actions

    def drop(self, keys, point, actions):
        effect = DWORD(actions)
        self.entered = False  # Drop ends the session, including a Shell cancellation.
        result = call(self.target, 6, HRESULT, (PTR, DWORD, POINT, PTR),
                      self.data, keys, point, C.byref(effect))
        check(result, 'IDropTarget.Drop')
        return result == 0

    def close(self):
        if self.entered:
            call(self.target, 5, HRESULT, ())  # IDropTarget.DragLeave
        call(self.target, 2, W.ULONG, ())
        call(self.data, 2, W.ULONG, ())


class ApplicationDrop:
    def __init__(self, shell, executable, keys, owner, arguments='', directory='', show=1):
        self.shell = shell
        # Preserve the shortcut's command line and quote the added paths separately.
        parameters = ' '.join(filter(None, (arguments, list2cmdline(keys))))
        self.info = ShellExecuteInfo(size=C.sizeof(ShellExecuteInfo), mask=0x400,  # SEE_MASK_FLAG_NO_UI
                                     window=owner, verb='open', file=str(executable), parameters=parameters,
                                     directory=directory or str(executable.parent), show=show)

    def update(self, keys, point, actions):
        return (actions & 1) or (actions & 4)  # DROPEFFECT_COPY or DROPEFFECT_LINK.

    def drop(self, keys, point, actions):
        if not self.shell.ShellExecuteExW(C.byref(self.info)):
            raise C.WinError(C.get_last_error())
        return True

    def close(self):
        pass  # No COM drag session to release.


class ExplorerDesktop:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('桌面整理需要 Windows 资源管理器')
        self.ole = C.OleDLL('ole32')
        self.user = C.WinDLL('user32', use_last_error=True)
        self.shell = C.WinDLL('shell32', use_last_error=True)
        self.interfaces = []
        self.items_by_name = {}
        self.item_indices = {}
        # WinDLL preserves the HRESULT so an already-initialized STA is handled normally.
        ole = C.WinDLL('ole32')
        result = ole.CoInitializeEx(None, 2)
        check(result, 'CoInitializeEx')
        self.initialized = True
        try:
            self.connect()
        except BaseException:
            self.close()
            raise

    def acquire(self, interface, index, types, *args, operation):
        pointer = PTR()
        check(call(interface, index, HRESULT, (*types, P_PTR), *args, C.byref(pointer)), operation)
        if not pointer:
            raise OSError(f'{operation} 返回了空接口')
        self.interfaces.append(pointer)
        return pointer

    def query(self, interface, iid):
        return self.acquire(interface, 0, (PTR,), C.byref(GUID(iid)), operation='QueryInterface')

    def connect(self):
        iid = GUID('85cb6900-4d95-11cf-960c-0080c7f4ee85')
        clsid = GUID('9ba05972-f6a8-11cf-a442-00a0c90a8f39')
        windows = PTR()
        self.ole.CoCreateInstance(C.byref(clsid), None, 4, C.byref(iid), C.byref(windows))
        if not windows:
            raise OSError('无法连接 Windows 桌面')
        self.interfaces.append(windows)

        location = Variant()
        location.kind = 3  # VT_I4, CSIDL_DESKTOP is zero.
        reserved = Variant()
        hwnd = W.LONG()
        dispatch = self.acquire(windows, 15, (PTR, PTR, C.c_int, PTR, C.c_int), C.byref(location),
                                C.byref(reserved), 8, C.byref(hwnd), 1, operation='IShellWindows.FindWindowSW')
        provider = self.query(dispatch, '6d5140c1-7436-11ce-8034-00aa006009fa')
        service = GUID('4c96be40-915c-11cf-99d3-00aa004ae837')
        browser_iid = GUID('000214e2-0000-0000-c000-000000000046')
        browser = self.acquire(provider, 3, (PTR, PTR), C.byref(service), C.byref(browser_iid),
                               operation='IServiceProvider.QueryService')
        view = self.acquire(browser, 15, (), operation='IShellBrowser.QueryActiveShellView')
        self.view = self.query(view, '1af3a467-214f-4298-908e-06b03e0b39f9')
        self.options = self.query(view, '3cc974d2-b302-4d36-ad3e-06d93f695d3f')
        # Build menus in this process so Shell extensions receive normal menu messages.
        self.desktop_folder = PTR()
        self.shell.SHGetDesktopFolder.argtypes = (P_PTR,)
        self.shell.SHGetDesktopFolder.restype = HRESULT
        check(self.shell.SHGetDesktopFolder(C.byref(self.desktop_folder)), 'SHGetDesktopFolder')
        self.interfaces.append(self.desktop_folder)
        view_hwnd = W.HWND()
        check(call(view, 3, HRESULT, (PTR,), C.byref(view_hwnd)), 'IShellView.GetWindow')
        find_child = self.user.FindWindowExW
        find_child.argtypes = (W.HWND, W.HWND, W.LPCWSTR, W.LPCWSTR)
        find_child.restype = W.HWND
        self.icon_view = find_child(view_hwnd, None, 'SysListView32', None)
        self.coordinates = self.icon_view or view_hwnd.value
        self.user.GetWindowRgn.argtypes = (W.HWND, W.HANDLE)
        self.user.SetWindowRgn.argtypes = (W.HWND, W.HANDLE, W.BOOL)
        self.gdi = C.WinDLL('gdi32', use_last_error=True)
        self.gdi.CreateRectRgn.argtypes = (C.c_int,) * 4
        self.gdi.CreateRectRgn.restype = W.HANDLE
        self.gdi.CombineRgn.argtypes = (W.HANDLE, W.HANDLE, W.HANDLE, C.c_int)
        self.gdi.GetRegionData.argtypes = (W.HANDLE, DWORD, PTR)
        self.gdi.GetRegionData.restype = DWORD
        self.gdi.DeleteObject.argtypes = (W.HANDLE,)
        self.oleacc = C.WinDLL('oleacc')
        self.oleacc.AccessibleObjectFromWindow.argtypes = (W.HWND, DWORD, PTR, P_PTR)
        self.oleacc.AccessibleObjectFromWindow.restype = HRESULT
        self.user.GetWindowLongW.argtypes = (W.HWND, C.c_int)
        self.user.GetWindowLongW.restype = W.LONG
        self.user.IsWindow.argtypes = (W.HWND,)
        self.user.ShowWindow.argtypes = (W.HWND, C.c_int)
        self.shell.ShellExecuteExW.argtypes = (C.POINTER(ShellExecuteInfo),)
        self.shell.ShellExecuteExW.restype = W.BOOL
        self.user.ClientToScreen.argtypes = (W.HWND, C.POINTER(POINT))
        self.user.GetCursorPos.argtypes = (C.POINTER(POINT),)
        self.user.GetWindow.argtypes = (W.HWND, C.c_uint)
        self.user.GetWindow.restype = W.HWND
        self.user.SetWindowPos.argtypes = (W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, C.c_uint)
        self.user.SetWindowPos.restype = W.BOOL
        self.user.GetWindowRect.argtypes = (W.HWND, C.POINTER(W.RECT))
        self.user.GetClientRect.argtypes = (W.HWND, C.POINTER(W.RECT))
        self.shell.SHGetNameFromIDList.argtypes = (PTR, C.c_uint, P_PTR)
        self.shell.SHGetNameFromIDList.restype = HRESULT
        self.shell.SHParseDisplayName.argtypes = (W.LPCWSTR, PTR, P_PTR, DWORD, PTR)
        self.shell.SHParseDisplayName.restype = HRESULT
        self.shell.SHBindToParent.argtypes = (PTR, PTR, P_PTR, P_PTR)
        self.shell.SHBindToParent.restype = HRESULT
        self.shell.SHCreateShellItemArrayFromIDLists.argtypes = (C.c_uint, PTR, P_PTR)
        self.shell.SHCreateShellItemArrayFromIDLists.restype = HRESULT
        self.shell.ILGetSize.argtypes = (PTR,)
        self.shell.ILGetSize.restype = C.c_uint
        self.ole.CoTaskMemFree.argtypes = (PTR,)
        self.ole.CoTaskMemFree.restype = None

    def free_items(self):
        for pidl in self.items_by_name.values():
            self.ole.CoTaskMemFree(pidl)
        self.items_by_name.clear()
        self.item_indices.clear()

    def close(self):
        self.free_items()
        while self.interfaces:
            call(self.interfaces.pop(), 2, W.ULONG, ())
        if self.initialized:
            C.WinDLL('ole32').CoUninitialize()
            self.initialized = False

    def flags(self):
        flags = DWORD()
        options = DWORD()
        check(call(self.view, 25, HRESULT, (PTR,), C.byref(flags)), 'IFolderView2.GetCurrentFolderFlags')
        check(call(self.options, 4, HRESULT, (PTR,), C.byref(options)), 'IFolderViewOptions.GetFolderViewOptions')
        return flags.value, options.value

    def set_flags(self, flags, options):
        current_flags, current_options = self.flags()
        changed_flags = (current_flags ^ flags) & LAYOUT_FLAGS
        custom = current_options & FVO_CUSTOMPOSITION
        target_custom = options & FVO_CUSTOMPOSITION
        explicit_custom = not current_options & FVO_VISTALAYOUT
        custom_supported = True
        # Vista layout already provides custom positions. Explorer may reject
        # the explicit option setter for that view with E_NOTIMPL.
        if explicit_custom and not custom and (changed_flags or target_custom):
            result = call(self.options, 3, HRESULT, (DWORD, DWORD), FVO_CUSTOMPOSITION, FVO_CUSTOMPOSITION)
            custom_supported = result & 0xffffffff != E_NOTIMPL
            if custom_supported:
                check(result, 'IFolderViewOptions.SetFolderViewOptions')
                custom = FVO_CUSTOMPOSITION
        try:
            if changed_flags:
                result = call(self.view, 24, HRESULT, (DWORD, DWORD), changed_flags, flags & changed_flags)
                if result & 0xffffffff != E_NOTIMPL or changed_flags & FWF_AUTOARRANGE:
                    check(result, 'IFolderView2.SetCurrentFolderFlags')
        finally:
            if explicit_custom and custom_supported and custom != target_custom:
                check(call(self.options, 3, HRESULT, (DWORD, DWORD), FVO_CUSTOMPOSITION, target_custom),
                      'IFolderViewOptions.SetFolderViewOptions')

    def name_from_id(self, pidl, kind):
        name = PTR()
        if self.shell.SHGetNameFromIDList(pidl, kind, C.byref(name)) >= 0 and name:
            try:
                return C.wstring_at(name)
            finally:
                self.ole.CoTaskMemFree(name)
        return ''

    def icon_name(self, pidl):
        # The desktop folder is the root of the Shell namespace.
        return self.name_from_id(pidl, 0x80058000) or self.name_from_id(pidl, 0x80028000)

    def item_count(self):
        count = C.c_int()
        check(call(self.view, 7, HRESULT, (C.c_uint, PTR), SVGIO_ALLVIEW, C.byref(count)), 'IFolderView.ItemCount')
        return count.value

    def positions(self, desired=None):
        """Read positions and refresh the Shell item identifiers for scoped moves."""
        items = {}
        identifiers = {}
        indices = {}
        allocated = []
        try:
            for index in range(self.item_count()):
                pidl = PTR()
                result = call(self.view, 6, HRESULT, (C.c_int, P_PTR), index, C.byref(pidl))
                if result < 0 or not pidl:
                    continue
                allocated.append(pidl)
                name = self.icon_name(pidl)
                if not name:
                    continue
                key = os.path.normcase(os.path.normpath(name))
                identifiers[key] = pidl
                indices[key] = index
                point = POINT()
                if call(self.view, 11, HRESULT, (PTR, PTR), pidl, C.byref(point)) >= 0:
                    items[key] = (point.x, point.y)
            self.free_items()
            self.items_by_name = identifiers
            self.item_indices = indices
            if desired is not None:
                self.position_icons({key: point for key, point in desired.items() if items.get(key) != tuple(point)})
            return items
        finally:
            retained = {pidl.value for pidl in self.items_by_name.values()}
            for pidl in allocated:
                if pidl.value not in retained:
                    self.ole.CoTaskMemFree(pidl)

    def position_icons(self, desired):
        """Update only the requested icons using the last Explorer enumeration."""
        moves = [(self.items_by_name[key], point) for key, point in desired.items() if key in self.items_by_name]
        if moves:
            identifiers = (PTR * len(moves))(*(pidl.value for pidl, _ in moves))
            points = (POINT * len(moves))(*(POINT(*point) for _, point in moves))
            check(call(self.view, 16, HRESULT, (C.c_uint, PTR, PTR, DWORD),
                       len(moves), identifiers, points, SVSI_POSITIONITEM), 'IFolderView.SelectAndPositionItems')

    def spacing(self):
        point = POINT()
        check(call(self.view, 12, HRESULT, (PTR,), C.byref(point)), 'IFolderView.GetSpacing')
        return point.x, point.y

    def desktop_size(self):
        rect = W.RECT()
        if not self.user.GetClientRect(self.coordinates, C.byref(rect)):
            raise C.WinError(C.get_last_error())
        return rect.right, rect.bottom

    def window_bounds(self, hwnd):
        """Return a window's rectangle in Explorer's icon coordinates."""
        rect, origin = W.RECT(), POINT()
        if (not self.user.GetWindowRect(hwnd, C.byref(rect))
                or not self.user.ClientToScreen(self.coordinates, C.byref(origin))):
            raise C.WinError(C.get_last_error())
        return rect.left - origin.x, rect.top - origin.y, rect.right - rect.left, rect.bottom - rect.top

    def raise_partition(self, window, siblings):
        """Stack this partition above its peers in the existing desktop window band."""
        remaining = set(siblings)
        highest = None
        current = self.user.GetWindow(window, 3)  # GW_HWNDPREV
        while current and remaining:
            if current in remaining:
                highest = current
                remaining.remove(current)
            current = self.user.GetWindow(current, 3)
        if highest is not None:
            preceding = self.user.GetWindow(highest, 3)
            # Keep the window non-activating and bypass Qt's bottom-placement hint
            # for this one relative z-order change.
            flags = 0x1 | 0x2 | 0x10 | 0x200 | 0x400  # NOSIZE, NOMOVE, NOACTIVATE, NOOWNERZORDER, NOSENDCHANGING.
            if not self.user.SetWindowPos(window, preceding, 0, 0, 0, 0, flags):
                raise C.WinError(C.get_last_error())

    def icon_rectangles(self, keys):
        """Read the bounds of the native icon and its label in list-view coordinates."""
        if not keys:
            return {}
        accessible = PTR()
        iid = GUID('618736e0-3c3d-11cf-810c-00aa00389b71')
        check(self.oleacc.AccessibleObjectFromWindow(self.icon_view, 0xfffffffc, C.byref(iid),
                                                   C.byref(accessible)), 'AccessibleObjectFromWindow')
        try:
            origin = POINT()
            if not self.user.ClientToScreen(self.coordinates, C.byref(origin)):
                raise C.WinError(C.get_last_error())
            rectangles = {}
            for key in keys:
                index = self.item_indices.get(key)
                if index is None:
                    continue
                child = Variant()
                child.kind = 3  # VT_I4, MSAA child IDs are one-based.
                child.data.integer = index + 1
                x, y, width, height = (W.LONG() for _ in range(4))
                check(call(accessible, 22, HRESULT, (PTR, PTR, PTR, PTR, Variant), C.byref(x),
                           C.byref(y), C.byref(width), C.byref(height), child), 'IAccessible.accLocation')
                if width.value > 0 and height.value > 0:
                    left, top = x.value - origin.x, y.value - origin.y
                    rectangles[key] = (left - 2, top - 2, left + width.value + 2, top + height.value + 2)
            return rectangles
        finally:
            call(accessible, 2, W.ULONG, ())

    def new_region(self, rectangle=(0, 0, 0, 0)):
        region = self.gdi.CreateRectRgn(*rectangle)
        if not region:
            raise C.WinError(C.get_last_error())
        return region

    def icon_region(self):
        if not self.icon_view:
            raise OSError('无法访问 Windows 桌面图标层')
        region = self.new_region()
        try:
            if not self.user.GetWindowRgn(self.icon_view, region):
                return None
            size = self.gdi.GetRegionData(region, 0, None)
            if not size:
                raise C.WinError(C.get_last_error())
            data = C.create_string_buffer(size)
            if self.gdi.GetRegionData(region, size, data) != size:
                raise C.WinError(C.get_last_error())
            header_size, _, count, _ = struct.unpack_from('<IIII', data)
            return tuple(struct.unpack_from('<4i', data, header_size + 16 * index) for index in range(count))
        finally:
            self.gdi.DeleteObject(region)

    def set_icon_region(self, original, excluded=()):
        """Mask only grouped artwork, leaving the native desktop available elsewhere."""
        if not self.icon_view:
            raise OSError('无法访问 Windows 桌面图标层')
        if original is None and not excluded:
            if not self.user.SetWindowRgn(self.icon_view, None, True):
                raise C.WinError(C.get_last_error())
            return
        region = self.new_region()
        try:
            if original is None:
                width, height = self.desktop_size()
                original = ((0, 0, width, height),)
            for rectangles, operation in ((original, 2), (excluded, 4)):  # RGN_OR, RGN_DIFF
                for rectangle in rectangles:
                    part = self.new_region(rectangle)
                    try:
                        if not self.gdi.CombineRgn(region, region, part, operation):
                            raise C.WinError(C.get_last_error())
                    finally:
                        self.gdi.DeleteObject(part)
            if not self.user.SetWindowRgn(self.icon_view, region, True):
                raise C.WinError(C.get_last_error())
            region = None  # Windows owns the region after SetWindowRgn succeeds.
        finally:
            if region:
                self.gdi.DeleteObject(region)

    def icon_size(self):
        mode, size = C.c_int(), C.c_int()
        result = call(self.view, 36, HRESULT, (PTR, PTR), C.byref(mode), C.byref(size))
        return size.value if result >= 0 and size.value > 0 else 32

    def icons_visible(self):
        if not self.icon_view or not self.user.IsWindow(self.icon_view):
            raise OSError('无法访问 Windows 桌面图标层')
        return bool(self.user.GetWindowLongW(self.icon_view, -16) & 0x10000000)  # WS_VISIBLE

    def set_icons_visible(self, visible):
        if self.icons_visible() == visible:
            return
        # Hide only this Explorer window's icon control. The user's desktop
        # settings, file attributes, and Shell item positions are untouched.
        self.user.ShowWindow(self.icon_view, 8 if visible else 0)  # SW_SHOWNA / SW_HIDE
        if self.icons_visible() != visible:
            raise OSError('无法切换 Windows 桌面图标层')

    @staticmethod
    def item_ui_object(folder, pidls, owner, iid):
        interface = PTR()
        array = (PTR * len(pidls))(*(pidl.value for pidl in pidls))
        check(call(folder, 10, HRESULT, (W.HWND, C.c_uint, PTR, PTR, PTR, P_PTR),
                   owner, len(pidls), array, C.byref(GUID(iid)), None, C.byref(interface)),
              'IShellFolder.GetUIObjectOf')
        if not interface:
            raise OSError('IShellFolder.GetUIObjectOf 返回了空接口')
        return interface

    def item_context_menu(self, keys, owner):
        pidls = [self.items_by_name[key] for key in keys if key in self.items_by_name]
        if not pidls:
            raise OSError('桌面项目已被移除')
        return self.item_ui_object(self.desktop_folder, pidls, owner, '000214e4-0000-0000-c000-000000000046')

    def shell_id_list(self, keys):
        """Encode the desktop PIDLs as CFSTR_SHELLIDLIST for native Explorer drops."""
        if any(key not in self.items_by_name for key in keys):
            return b''
        pidls = [b'\0\0']  # The parent PIDL is the desktop namespace root.
        for key in keys:
            pidl = self.items_by_name[key]
            pidls.append(C.string_at(pidl, self.shell.ILGetSize(pidl)))
        offset = 4 * (1 + len(pidls))
        offsets = []
        for pidl in pidls:
            offsets.append(offset)
            offset += len(pidl)
        return struct.pack(f'<{len(offsets) + 1}I', len(keys), *offsets) + b''.join(pidls)

    def drop_data(self, keys, owner):
        pidls, parsed = [], []
        try:
            for key in keys:
                pidl = self.items_by_name.get(key)
                if pidl is None:
                    pidl = PTR()
                    check(self.shell.SHParseDisplayName(key, None, C.byref(pidl), 0, None), 'SHParseDisplayName')
                    parsed.append(pidl)
                pidls.append(pidl)
            parent = PureWindowsPath(keys[0]).parent
            if parent.is_absolute() and all(PureWindowsPath(key).parent == parent for key in keys):
                # The desktop root prefers creating links. A flat file selection
                # gets its normal volume- and modifier-dependent effect instead.
                array = (PTR * len(pidls))(*(pidl.value for pidl in pidls))
                items, data = PTR(), PTR()
                check(self.shell.SHCreateShellItemArrayFromIDLists(len(array), array, C.byref(items)),
                      'SHCreateShellItemArrayFromIDLists')
                try:
                    handler = GUID('b8c0bd9f-ed24-455c-83e6-d5390c4fe8c4')  # BHID_DataObject
                    iid = GUID('0000010e-0000-0000-c000-000000000046')  # IDataObject
                    check(call(items, 3, HRESULT, (PTR, PTR, PTR, P_PTR),
                               None, C.byref(handler), C.byref(iid), C.byref(data)), 'IShellItemArray.BindToHandler')
                    return data
                finally:
                    call(items, 2, W.ULONG, ())
            return self.item_ui_object(self.desktop_folder, pidls, owner, '0000010e-0000-0000-c000-000000000046')
        finally:
            for pidl in parsed:
                self.ole.CoTaskMemFree(pidl)

    def shortcut_application_drop(self, folder, child, keys, owner):
        link = self.item_ui_object(folder, (child,), owner, '000214f9-0000-0000-c000-000000000046')  # IShellLinkW
        try:
            path = C.create_unicode_buffer(32768)
            check(call(link, 3, HRESULT, (PTR, C.c_int, PTR, DWORD), path, len(path), None, 0),
                  'IShellLinkW.GetPath')
            executable = PureWindowsPath(path.value)
            if not (executable.is_absolute() and executable.suffix.lower() == '.exe') or os.path.isdir(executable):
                return None
            arguments = C.create_unicode_buffer(32768)
            directory = C.create_unicode_buffer(32768)
            show = C.c_int(1)
            check(call(link, 10, HRESULT, (PTR, C.c_int), arguments, len(arguments)), 'IShellLinkW.GetArguments')
            check(call(link, 8, HRESULT, (PTR, C.c_int), directory, len(directory)), 'IShellLinkW.GetWorkingDirectory')
            check(call(link, 14, HRESULT, (PTR,), C.byref(show)), 'IShellLinkW.GetShowCmd')
            return ApplicationDrop(self.shell, executable, keys, owner, arguments.value,
                                   os.path.expandvars(directory.value), show.value)
        finally:
            call(link, 2, W.ULONG, ())

    def item_drop(self, key, keys, owner):
        pidl = self.items_by_name.get(key)
        if pidl is None:
            return None
        path = PureWindowsPath(key)
        suffix = path.suffix.lower()
        file_drop = path.is_absolute() and all(PureWindowsPath(source).is_absolute() for source in keys)
        if file_drop and suffix == '.exe' and not os.path.isdir(path):
            return ApplicationDrop(self.shell, path, keys, owner)
        parent, child = PTR(), PTR()
        iid = GUID('000214e6-0000-0000-c000-000000000046')  # IShellFolder
        check(self.shell.SHBindToParent(pidl, C.byref(iid), C.byref(parent), C.byref(child)), 'SHBindToParent')
        try:
            if file_drop and suffix == '.lnk' and not os.path.isdir(path):
                operation = self.shortcut_application_drop(parent, child, keys, owner)
                if operation is not None:
                    return operation
            target = self.item_ui_object(parent, (child,), owner, '00000122-0000-0000-c000-000000000046')
        finally:
            call(parent, 2, W.ULONG, ())
        try:
            return ShellDrop(target, self.drop_data(keys, owner))
        except BaseException:
            call(target, 2, W.ULONG, ())
            raise

    def cursor_position(self):
        point = POINT()
        if not self.user.GetCursorPos(C.byref(point)):
            raise C.WinError(C.get_last_error())
        return point

    def track_context_menu(self, context, menu, owner, point):
        common = C.WinDLL('comctl32', use_last_error=True)
        result_type = C.c_ssize_t
        subclass_type = C.WINFUNCTYPE(result_type, W.HWND, C.c_uint, W.WPARAM, W.LPARAM,
                                     C.c_size_t, C.c_size_t)
        common.SetWindowSubclass.argtypes = (W.HWND, subclass_type, C.c_size_t, C.c_size_t)
        common.RemoveWindowSubclass.argtypes = (W.HWND, subclass_type, C.c_size_t)
        common.DefSubclassProc.argtypes = (W.HWND, C.c_uint, W.WPARAM, W.LPARAM)
        common.DefSubclassProc.restype = result_type
        menu_messages = PTR()
        has_result = False
        for guid in ('bcfce0a0-ec17-11d0-8d10-00a0c90f2719', '000214f4-0000-0000-c000-000000000046'):
            if call(context, 0, HRESULT, (PTR, P_PTR), C.byref(GUID(guid)), C.byref(menu_messages)) >= 0:
                has_result = guid.startswith('bcfce0a0')
                break

        @subclass_type
        def forward(window, message, wparam, lparam, subclass_id, data):
            if menu_messages and message in (0x0117, 0x002B, 0x002C, 0x0120):
                if has_result:
                    result = result_type()
                    status = call(menu_messages, 7, HRESULT, (C.c_uint, W.WPARAM, W.LPARAM, PTR),
                                  message, wparam, lparam, C.byref(result))
                    if status == 0:
                        return result.value
                elif message != 0x0120:
                    status = call(menu_messages, 6, HRESULT, (C.c_uint, W.WPARAM, W.LPARAM), message, wparam, lparam)
                    if status == 0:
                        return 0
            return common.DefSubclassProc(window, message, wparam, lparam)

        attached = False
        try:
            if menu_messages:
                attached = bool(common.SetWindowSubclass(owner, forward, 1, 0))
                if not attached:
                    raise C.WinError(C.get_last_error())
            popup = self.user.TrackPopupMenuEx
            popup.argtypes = (W.HMENU, C.c_uint, C.c_int, C.c_int, W.HWND, PTR)
            popup.restype = C.c_uint
            position = POINT(*point)
            if not self.user.ClientToScreen(owner, C.byref(position)):
                raise C.WinError(C.get_last_error())
            return popup(menu, 0x0102, position.x, position.y, owner, None)  # TPM_RETURNCMD | TPM_RIGHTBUTTON
        finally:
            if attached:
                common.RemoveWindowSubclass(owner, forward, 1)
            if menu_messages:
                call(menu_messages, 2, W.ULONG, ())

    def show_item_menu(self, keys, owner, point):
        context = self.item_context_menu(keys, owner)
        user = self.user
        user.CreatePopupMenu.restype = W.HMENU
        user.DestroyMenu.argtypes = (W.HMENU,)
        menu = user.CreatePopupMenu()
        try:
            if not menu:
                raise C.WinError(C.get_last_error())
            first, last = 1, 0x6FFF
            check(call(context, 3, HRESULT, (W.HMENU, C.c_uint, C.c_uint, C.c_uint, C.c_uint),
                       menu, 0, first, last, 0x10), 'IContextMenu.QueryContextMenu')  # CMF_CANRENAME
            command = self.track_context_menu(context, menu, owner, point)
            if not first <= command <= last:
                return None
            verb = C.create_unicode_buffer(128)
            offset = command - first
            if call(context, 5, HRESULT, (C.c_size_t, C.c_uint, PTR, PTR, C.c_uint),
                    offset, 4, None, verb, len(verb)) >= 0 and verb.value.casefold() == 'rename':
                return 'rename'
            invocation = InvokeCommandInfo(size=C.sizeof(InvokeCommandInfo), owner=owner, verb=offset, show=1)
            check(call(context, 4, HRESULT, (PTR,), C.byref(invocation)), 'IContextMenu.InvokeCommand')
            return 'refresh'
        finally:
            if menu:
                user.DestroyMenu(menu)
            call(context, 2, W.ULONG, ())

    def edit_name(self, key):
        pidl = self.items_by_name.get(key)
        if pidl is None:
            raise OSError('桌面项目已被移除')
        return self.name_from_id(pidl, 0x80031001) or PureWindowsPath(key).name  # SIGDN_PARENTRELATIVEEDITING

    def rename_item(self, key, name, owner):
        pidl = self.items_by_name.get(key)
        if pidl is None:
            raise OSError('桌面项目已被移除')
        renamed = PTR()
        check(call(self.desktop_folder, 12, HRESULT, (W.HWND, PTR, W.LPCWSTR, C.c_uint, P_PTR),
                   owner, pidl, name, 0x1001, C.byref(renamed)), 'IShellFolder.SetNameOf')
        try:
            updated = self.icon_name(renamed) if renamed else ''
            return os.path.normcase(os.path.normpath(updated)) if updated else key
        finally:
            if renamed:
                self.ole.CoTaskMemFree(renamed)

    def open_item(self, key):
        pidl = self.items_by_name.get(key)
        if pidl is None:
            raise OSError('桌面项目已经移除')
        info = ShellExecuteInfo(size=C.sizeof(ShellExecuteInfo), mask=0x4, show=1,
                                id_list=pidl.value)  # SEE_MASK_IDLIST
        if not self.shell.ShellExecuteExW(C.byref(info)):
            raise C.WinError(C.get_last_error())
