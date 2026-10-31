"""Local searchable group selection; reads dialogs without starting capture."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import queue
import threading

from .filters import CaptureFilter, load_filter, save_filter
from .groups import SelectionModel, fetch_groups


def persist_selection(model, path, *, enabled=True):
    configuration = CaptureFilter(enabled, model.selected)
    save_filter(path, configuration)
    return configuration


async def read_catalog(args):
    from .capture import load_schema
    from .client import TatoConnection
    from .live_wire import TatoUpdatesRegistry
    from .session_store import load_session

    session = load_session(args.session)
    profile = json.loads(args.profile.read_text(encoding='utf-8'))
    public_key = args.public_key.read_bytes()
    logger = logging.getLogger('tatocollector.transport')
    previous_disabled = logger.disabled
    logger.disabled = True  # SDK log records may contain raw account data.
    try:
        # The read-only selector does not consume or write the collector's
        # queue/cursor. Ignore its temporary connection's unsolicited updates;
        # normal collector difference recovery remains responsible for them.
        with TatoUpdatesRegistry():
            async with TatoConnection(profile, public_key, host=session['host'], port=session['port'],
                                      dc_id=session['dc_id'], auth_key=bytes.fromhex(session['auth_key_hex']),
                                      time_offset=session['time_offset'], updates_queue=asyncio.Queue(maxsize=16)) as connection:
                return await fetch_groups(connection, load_schema())
    finally:
        logger.disabled = previous_disabled


class PickerWindow:
    """Thin Tk view; selection survives searches and directory omissions."""

    _KIND_LABEL = {'group': '普通群', 'supergroup': '超级群', 'channel': '频道',
                   'unavailable': '已选（不可见）'}

    def __init__(self, root, entries, current_filter, config_path):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.config_path = root, Path(config_path)
        self.model = SelectionModel(entries, current_filter.peers)
        self.result = None
        self.root.title('选择定向捕获群组')
        self.root.geometry('880x600')
        self.root.minsize(700, 450)
        self.root.protocol('WM_DELETE_WINDOW', self.cancel)
        self.frame = ttk.Frame(root, padding=16)
        self.frame.pack(fill='both', expand=True)
        mode = '定向捕获' if current_filter.enabled else '全量捕获'
        ttk.Label(self.frame, text='选择需要捕获消息的群组和频道', font=('', 13, 'bold')).pack(anchor='w')
        ttk.Label(self.frame, text=f'当前配置：{mode}。点击行或按空格勾选；搜索不会清除已选项。').pack(anchor='w', pady=(4, 12))
        search = ttk.Frame(self.frame)
        search.pack(fill='x')
        ttk.Label(search, text='搜索群名或 ID').pack(side='left')
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.pack(side='left', fill='x', expand=True, padx=(10, 0))
        commands = ttk.Frame(self.frame)
        commands.pack(fill='x', pady=(8, 8))
        ttk.Button(commands, text='全选当前结果', command=lambda: self.set_visible(True)).pack(side='left')
        ttk.Button(commands, text='取消当前结果', command=lambda: self.set_visible(False)).pack(side='left', padx=6)
        ttk.Button(commands, text='查看不可见已选', command=lambda: self.search_var.set('不可见')).pack(side='left')
        table = ttk.Frame(self.frame)
        table.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(table, columns=('checked', 'title', 'kind', 'identity'),
                                 show='headings', selectmode='browse')
        for name, label, width in (('checked', '选择', 48), ('title', '群组／频道名称', 390),
                                   ('kind', '类型', 120), ('identity', '会话 ID', 170)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=width if name != 'title' else 160,
                             stretch=name == 'title', anchor='w')
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.tree.bind('<Button-1>', self.on_click)
        self.tree.bind('<space>', self.on_space)
        self.status_var = tk.StringVar()
        ttk.Label(self.frame, textvariable=self.status_var).pack(anchor='w', pady=(10, 4))
        ttk.Label(self.frame, text='保存空名单会停止新增消息入队；关闭筛选才会恢复全部会话。').pack(anchor='w')
        buttons = ttk.Frame(self.frame)
        buttons.pack(fill='x', pady=(12, 0))
        ttk.Button(buttons, text='关闭筛选（全量捕获）', command=self.disable_filter).pack(side='left')
        ttk.Button(buttons, text='取消', command=self.cancel).pack(side='right')
        ttk.Button(buttons, text='保存定向名单', command=self.save_selection).pack(side='right', padx=8)
        self.search_var.trace_add('write', lambda *_: self.refresh())
        self.refresh()
        self.search_entry.focus_set()

    @staticmethod
    def _identity(entry):
        return f"{entry['chat_type']}:{entry['chat_id']}"

    def refresh(self):
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        for entry in self.model.visible(self.search_var.get()):
            key = entry['chat_type'], entry['chat_id']
            self.tree.insert('', 'end', iid=self._identity(entry), values=(
                '[x]' if key in self.model.selected else '[ ]', entry['title'],
                self._KIND_LABEL.get(entry['kind'], '群组'), self._identity(entry)))
        if focus and self.tree.exists(focus):
            self.tree.focus(focus)
        self.update_status()

    def update_status(self):
        missing = sum(entry.get('unavailable', False) for entry in self.model.visible()
                      if (entry['chat_type'], entry['chat_id']) in self.model.selected)
        self.status_var.set(f'已选 {len(self.model.selected)} 个会话；当前显示 {len(self.tree.get_children())} 个；不可见已选 {missing} 个。')

    def toggle_item(self, identity):
        if not identity:
            return
        kind, identifier = identity.split(':', 1)
        key = kind, int(identifier)
        self.model.toggle(key)
        self.tree.set(identity, 'checked', '[x]' if key in self.model.selected else '[ ]')
        self.tree.focus(identity)
        self.update_status()

    def on_click(self, event):
        self.toggle_item(self.tree.identify_row(event.y))

    def on_space(self, event):
        self.toggle_item(self.tree.focus())
        return 'break'

    def set_visible(self, selected):
        self.model.set_visible(self.search_var.get(), selected)
        self.refresh()

    def _save(self, enabled):
        from tkinter import messagebox
        try:
            result = persist_selection(self.model, self.config_path, enabled=enabled)
        except (OSError, ValueError):
            messagebox.showerror('未保存', '名单无法保存。原配置未修改，请检查本机文件权限或减少所选数量。', parent=self.root)
            return
        self.result = result
        self.root.destroy()

    def save_selection(self):
        self._save(True)

    def disable_filter(self):
        self._save(False)

    def cancel(self):
        self.result = None
        self.root.destroy()


def launch_picker(entries, current_filter, config_path):
    import tkinter as tk
    root = tk.Tk()
    window = PickerWindow(root, entries, current_filter, config_path)
    root.mainloop()
    return window.result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--filter', type=Path, default=Path('config/capture-filter.local.json'))
    parser.add_argument('--session', type=Path, default=Path('sessions/default.dpapi'))
    parser.add_argument('--profile', type=Path, default=Path('config/client-profile.local.json'))
    parser.add_argument('--public-key', type=Path, default=Path('research/android-static/embedded-public-0.pem'))
    args = parser.parse_args(argv)
    try:
        current_filter = load_filter(args.filter)
    except (OSError, ValueError):
        print('现有筛选配置无法读取；未请求群组目录，也未修改名单。请先修复该配置。')
        return 1
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print('无法打开本机选择窗口，请确认 Python 已安装 Tk。未修改名单。')
        return 1
    try:
        root = tk.Tk()
    except (tk.TclError, RuntimeError):
        print('无法打开本机选择窗口。未读取群组目录，也未修改名单。')
        return 1
    root.title('读取群组目录')
    root.geometry('460x160')
    root.minsize(400, 150)
    progress = ttk.Frame(root, padding=20)
    progress.pack(fill='both', expand=True)
    label = ttk.Label(progress, text='正在读取已加入的群组／频道……')
    label.pack(anchor='w', pady=(0, 12))
    ttk.Label(progress, text='此步骤不启动采集，也不推送消息。').pack(anchor='w')
    ttk.Button(progress, text='取消', command=root.destroy).pack(anchor='e', pady=(12, 0))
    outcome = queue.Queue(maxsize=1)
    state = {'window': None, 'failed': False}

    def worker():
        try:
            outcome.put(('ready', asyncio.run(read_catalog(args))))
        except Exception as exc:
            # Keep untrusted exception text and raw response bytes out of UI/logs.
            outcome.put(('error', type(exc).__name__))

    def poll():
        try:
            kind, value = outcome.get_nowait()
        except queue.Empty:
            root.after(100, poll)
            return
        if kind == 'error':
            state['failed'] = True
            label.configure(text=f'群组目录读取失败（{value}）。\n名单未修改，请关闭后重试。')
        else:
            progress.destroy()
            state['window'] = PickerWindow(root, value, current_filter, args.filter)

    threading.Thread(target=worker, daemon=True, name='read-only-group-catalog').start()
    root.after(100, poll)
    root.mainloop()
    if state['failed']:
        return 1
    window = state['window']
    result = window.result if window is not None else None
    if result is None:
        print('已取消，原筛选配置保持不变。')
    elif result.enabled:
        print(f'已保存定向名单：{len(result.peers)} 个会话。采集器没有在此步骤启动。')
    else:
        print('已关闭筛选，下次采集将按全量模式运行。采集器没有在此步骤启动。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
