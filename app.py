import asyncio
import json
from playwright.async_api import async_playwright
from nicegui import ui, app
from typing import Dict
from datetime import datetime, timezone
from functools import partial
import subprocess
import sys
import os
import re

live_agent_stats = {}
name_filter_values = []
selected_names = []
name_filter_card = None
name_filter_visible = False
name_filter_query = ''
credentials = None
login_failed = False
switches = {}
subprocesses = []
ws_active = False

async def save_session(context):
    cookies = await context.cookies()
    with open("session.json", "w") as f:
        json.dump(cookies, f)

async def load_session(context):
    with open("session.json", "r") as f:
        cookies = json.load(f)
    await context.add_cookies(cookies)

def login_status_check():
    global login_failed
    if login_failed:
        login_status_label.text = 'Login failed. Please try again.'
        loading_container.classes(add='hidden')
        loading_label.text = ''
        login_container.classes(remove='hidden')
    else:
        login_status_label.text = ''

def terminate_processes():
    for process in subprocesses:
        process.terminate()

    app.shutdown()

def login_attempt():
    global credentials, login_failed
    login_failed = False
    loading_label.text = 'Attempting login...'
    login_container.classes(add='hidden')
    loading_container.classes(remove='hidden')
    credentials = {"username": input_u.value, "password": input_p.value}

def save_column_config():
    with open('column_config.json', 'w') as f:
        json.dump(columns_config, f)

def load_column_config():
    global columns_config
    try:
        with open('column_config.json', 'r') as f:
            loaded = json.load(f)
            if isinstance(loaded, list) and all('name' in col for col in loaded):
                # Ensure all required keys exist
                for col in loaded:
                    col.setdefault('classes', '')
                    col.setdefault('headerClasses', '')
                columns_config = loaded
    except FileNotFoundError:
        pass

def save_selected_names():
    with open('selected_names.json', 'w') as f:
        json.dump(selected_names, f)

def load_selected_names():
    try:
        with open('selected_names.json', 'r') as f:
            selected_names.extend(json.load(f))
    except FileNotFoundError:
        pass

def apply_column_order():
    table.columns = columns_config
    table.update()

def move_column(index: int, direction: int):
    new_index = index + direction
    if 0 <= new_index < len(columns_config):
        columns_config[index], columns_config[new_index] = columns_config[new_index], columns_config[index]
        apply_column_order()
        save_column_config()

def toggle(column: Dict, visible: bool) -> None:
    column['classes'] = '' if visible else 'hidden'
    column['headerClasses'] = '' if visible else 'hidden'
    apply_column_order()
    save_column_config()

def toggle_all_columns(value: bool):
    for column in columns_config:
        toggle(column, value)
    for sw in switches.values():
        if sw.value != value:
            sw.value = value

def handle_column_toggle(column, event):
    toggle(column, event.value)

def render_config_ui():
    config_panel.clear()
    switches.clear()
    with config_panel:
        ui.label('Column Configuration').classes('font-bold mb-2')
        ui.label('Column configuration will be retained on close of the app.').classes('text-red text-xs')
        all_visible = all(col.get('classes', '') == '' for col in columns_config)
        ui.switch("Select All", value=all_visible, on_change=lambda e: toggle_all_columns(e.value))

        for idx, col in enumerate(columns_config):
            visible = col.get('classes', '') == ''
            with ui.row().classes('w-full items-center justify-between'):
                switch = ui.switch(
                    col['label'],
                    on_change=partial(handle_column_toggle, col)
                )
                switch.value = visible  # <-- this line ensures correct state
                switches[col['name']] = switch

                with ui.row().classes('gap-1'):
                    ui.button(on_click=partial(move_column, idx, -1), icon='arrow_upward').classes('h-6 w-6 text-xs')
                    ui.button(on_click=partial(move_column, idx, 1), icon='arrow_downward').classes('h-6 w-6 text-xs')

def toggle_name_filter():
    global name_filter_visible
    name_filter_visible = not name_filter_visible
    if name_filter_visible:
        render_name_checkboxes()
        name_filter_card.classes(remove='hidden')
    else:
        name_filter_card.classes('hidden')

def update_filter_query(value: str):
    global name_filter_query
    name_filter_query = value
    render_name_checkboxes()

def render_name_checkboxes():
    name_checkbox_column.clear()
    filtered_names = [n for n in name_filter_values if name_filter_query.lower() in n.lower()]

    with name_checkbox_column:
        for name in filtered_names:
            def toggle_handler(e, name=name):
                if e.value and name not in selected_names:
                    selected_names.append(name)
                elif not e.value and name in selected_names:
                    selected_names.remove(name)
                
                save_selected_names()
                update_table()

            ui.checkbox(name, value=name in selected_names, on_change=toggle_handler)

def update_table():
    now = datetime.now(timezone.utc)

    names_set = set()
    for agent in live_agent_stats.values():
        try:
            start_time = datetime.fromisoformat(agent['enteredStateOn']).astimezone(timezone.utc)
            delta = now - start_time
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            agent['time_in_status'] = f"{hours:02}:{minutes:02}:{seconds:02}"
        except Exception:
            agent['time_in_status'] = "--:--:--"

        for raw_field, display_field in timestamp_fields.items():
            raw_value = agent.get(raw_field)
            try:
                dt = datetime.fromisoformat(raw_value)
                agent[display_field] = dt.strftime('%d/%m/%Y %H:%M:%S')
            except Exception:
                agent[display_field] = '--'

        names_set.add(agent['name'])

    # Only update filter options if we have new names
    global name_filter_values
    if set(name_filter_values) != names_set:
        name_filter_values.clear()
        name_filter_values.extend(sorted(names_set))
        render_name_checkboxes()

    # Apply filter if active
    if selected_names:
        filtered_agents = [a for a in live_agent_stats.values() if a['name'] in selected_names]
        table.rows = filtered_agents
    else:
        table.rows = list(live_agent_stats.values())

    table.update()


async def handle_frame(payload, inbound):
    try:
        data = json.loads(payload)
        if isinstance(data, dict) and "M" in data:
            for msg in data["M"]:
                if msg.get("M") == "onAgentStateChanged":
                    agent = msg["A"][0]
                    agent["name"] = f"{agent['firstName']} {agent['lastName']}"
                    live_agent_stats[agent["id"]] = agent
                    update_table()
    except Exception:
        pass

async def handle_websocket(ws):
    global ws_active
    ws_active = True
    ws.on("framereceived", lambda payload: asyncio.create_task(handle_frame(payload, inbound=True)))

async def playwright_worker():
    global credentials, login_failed

    async def poll_sse_messages(page):
        while True:
            if ws_active:
                break  # Stop polling if WebSocket is active
            messages = await page.evaluate("() => window._sse_messages.splice(0)")
            for msg in messages:
                await handle_frame(msg, inbound=True)
            await asyncio.sleep(0.1)

    login_window = subprocess.Popen([
            sys.executable,
            'webview_launcher.py',
            '--url', 'http://localhost:8080',
            '--title', 'Login',
            '--width', '400',
            '--height', '400',
            '--frameless'
    ])
    loading_label.text = 'Starting invisible browser...'
    loading_container.classes(remove='hidden')
    subprocesses.append(login_window)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")        
        
        if os.path.exists('storage.json'):
            context = await browser.new_context(storage_state='storage.json')
        else:
            context = await browser.new_context()

        page = await context.new_page()
        await page.add_init_script("""
            (() => {
                const originalEventSource = window.EventSource;
                window._sse_messages = [];

                window.EventSource = function (url, config) {
                    const sse = new originalEventSource(url, config);
                    sse.addEventListener('message', function (event) {
                        window._sse_messages.push(event.data);
                    });
                    return sse;
                };
            })();
        """)
        
        page.on("websocket", lambda ws: asyncio.create_task(handle_websocket(ws)))

        await page.goto("https://ccm01.lrg.co.uk/ignite", wait_until='domcontentloaded')

        while True:
            await asyncio.sleep(1)
            if credentials and await page.query_selector('#username'):
                loading_container.classes(add='hidden')
                login_container.classes(remove='hidden')
                try:
                    await page.fill('#username', credentials['username'])
                    await page.fill('input[type="password"]', credentials['password'])
                    await page.keyboard.press("Enter")
                    await page.wait_for_url(re.compile(r".*realtime.*"), timeout=5000)
                    await context.storage_state(path="storage.json")
                    loading_label.text = 'Login successful'
                    break
                except Exception:
                    login_failed = True
                    credentials = None
            elif not await page.query_selector('#username'):
                break
        
        asyncio.create_task(poll_sse_messages(page))

        try:
            await page.click('button[a-id="Cancel"]', timeout=2000)
        except Exception:
            pass

        await page.goto("https://ccm01.lrg.co.uk/ignite/#!/realtime/dashboard/")

        loading_label.text = 'Setting up Ignite...'

        await page.locator('a[a-id="EditDashboard"]').nth(0).click()
        
        try:
            await page.locator('a[a-id="Settings"]').nth(0).click(timeout=1000)
        except Exception:
            await page.locator('a[a-id="EditDashboard"]').nth(0).click()
            await page.locator('a[a-id="Settings"]').nth(0).click(timeout=1000)

        checkbox = await page.query_selector('input[a-id="VoiceSelectAllCheckbox"]')
        class_attr = await checkbox.get_attribute('class')

        if 'ng-not-empty' in class_attr.split():
            pass
        else:
            while True:
                try:
                    await page.locator('div[ng-click="showMore()"]').nth(0).click(timeout=1000)
                    await asyncio.sleep(1)
                except Exception:
                    break

            await page.click('input[type="checkbox"][a-id="VoiceSelectAllCheckbox"]')

            await page.click('input[type="submit"][value="Apply"]')

        loading_label.text = 'Launching app...'
        await asyncio.sleep(2)

        login_window.terminate()
        subprocesses.remove(login_window)

        loading_container.classes(add='hidden')
        login_container.classes(add='hidden')
        main_container.classes(remove='hidden')

        app_window = subprocess.Popen([
            sys.executable,
            'webview_launcher.py',
            '--url', 'http://localhost:8080',
            '--title', 'Ignite',
            '--width', '900',
            '--height', '600',
            '--resizable'
        ])

        while True:
            return_code = app_window.poll()
            if return_code is not None:
                save_column_config()
                save_selected_names()
                app.shutdown()
            await asyncio.sleep(1)

# ---------- UI ----------- #

columns_config = [
    {'name': 'name', 'label': 'Name', 'field': 'name'},
    {'name': 'reporting', 'label': 'Extension', 'field': 'reporting'},
    {'name': 'currentState', 'label': 'Current State', 'field': 'currentState'},
    {'name': 'formatted_enteredStateOn', 'label': 'Entered State On', 'field': 'formatted_enteredStateOn'},
    {'name': 'time_in_status', 'label': 'Time in Status', 'field': 'time_in_status'},
    {'name': 'reason', 'label': 'Reason', 'field': 'reason'},
    {'name': 'acdConversationsToday', 'label': 'ACD Conversations Today', 'field': 'acdConversationsToday'},
    {'name': 'nonAcdConversationsToday', 'label': 'Non-ACD Conversations Today', 'field': 'nonAcdConversationsToday'},
    {'name': 'occupiedDurationToday', 'label': 'Occupied Duration', 'field': 'occupiedDurationToday'},
    {'name': 'acdDurationToday', 'label': 'ACD Duration', 'field': 'acdDurationToday'},
    {'name': 'doNotDisturbDurationToday', 'label': 'DND Duration', 'field': 'doNotDisturbDurationToday'},
    {'name': 'holdAcdDurationToday', 'label': 'Hold ACD Duration', 'field': 'holdAcdDurationToday'},
    {'name': 'holdNonAcdDurationToday', 'label': 'Hold Non-ACD Duration', 'field': 'holdNonAcdDurationToday'},
    {'name': 'holdOutboundDurationToday', 'label': 'Hold Outbound Duration', 'field': 'holdOutboundDurationToday'},
    {'name': 'makeBusyDurationToday', 'label': 'Make Busy Duration', 'field': 'makeBusyDurationToday'},
    {'name': 'nonAcdDurationToday', 'label': 'Non-ACD Duration', 'field': 'nonAcdDurationToday'},
    {'name': 'outboundDurationToday', 'label': 'Outbound Duration', 'field': 'outboundDurationToday'},
    {'name': 'workTimerDurationToday', 'label': 'Work Timer Duration', 'field': 'workTimerDurationToday'},
    {'name': 'averageAnsweredDurationToday', 'label': 'Avg Answered Duration', 'field': 'averageAnsweredDurationToday'},
    {'name': 'loggedInDurationToday', 'label': 'Logged In Duration', 'field': 'loggedInDurationToday'},
    {'name': 'formatted_lastLoginTime', 'label': 'Last Login', 'field': 'formatted_lastLoginTime'},
    {'name': 'formatted_lastLogoffTime', 'label': 'Last Logoff', 'field': 'formatted_lastLogoffTime'},
    {'name': 'loggedInNotPresentDurationToday', 'label': 'Logged In Not Present Duration', 'field': 'loggedInNotPresentDurationToday'},
    {'name': 'externalAnswerDurationToday', 'label': 'External Answer Duration', 'field': 'externalAnswerDurationToday'},
    {'name': 'averageTime', 'label': 'Average Time', 'field': 'averageTime'},
    {'name': 'totalAcdDuration', 'label': 'Total ACD Duration', 'field': 'totalAcdDuration'},
    {'name': 'totalNonAcdDuration', 'label': 'Total Non-ACD Duration', 'field': 'totalNonAcdDuration'},
    {'name': 'unavailablePercentToday', 'label': 'Unavailable %', 'field': 'unavailablePercentToday'},
    {'name': 'outboundConversationsToday', 'label': 'Outbound Conversations', 'field': 'outboundConversationsToday'},
    {'name': 'externalOutboundConversationsToday', 'label': 'External Outbound Conversations', 'field': 'externalOutboundConversationsToday'},
    {'name': 'externalInboundConversationsToday', 'label': 'External Inbound Conversations', 'field': 'externalInboundConversationsToday'},
    {'name': 'availableState', 'label': 'Available State', 'field': 'availableState'},
]

for col in columns_config:
    col.setdefault('classes', '')
    col.setdefault('headerClasses', '')

with ui.column().classes('w-full h-screen items-center justify-center p-8 hidden') as login_container:
    login_label = ui.label('Ignite Login').classes('text-2xl')
    input_u = ui.input('Username').classes('w-full text-lg')
    input_p = ui.input('Password', password=True).classes('w-full text-lg')
    login_status_label = ui.label().classes('w-full')
    with ui.row().classes('w-full justify-center') as button_group:
        ui.button('Log In', on_click=login_attempt).classes('m-4')
        ui.button('Quit', on_click=terminate_processes).classes('m-4')

with ui.column().classes('w-full h-screen items-center justify-center p-8 hidden') as loading_container:
    loading = ui.spinner(size='xl')
    loading_label = ui.label()

with ui.column().classes('w-full h-screen p-4 hidden') as main_container:
    with ui.row().classes('w-full justify-between items-stretch'):
        with ui.column().classes('max-w-3xl'):
            ui.label('Ignite').classes('text-2xl font-bold')
            ui.label('All data is directly streamed from the Ignite portal via invisible browser.') \
                .classes('text-xs break-words')
        
        with ui.row().classes('flex-grow justify-end items-end'):
            ui.button('Filter Names', on_click=lambda: toggle_name_filter())
            with ui.button(text="Configure Columns", on_click=render_config_ui):
                with ui.menu(), ui.column().classes('gap-0 p-2'):
                    config_panel = ui.column().classes('gap-0')
                    render_config_ui()


    table = ui.table(
        columns=columns_config,
        column_defaults={'sortable': True},
        rows=[],
        row_key='id',
    ).classes('w-full').style('max-height: 88vh; overflow-y: auto;').classes('sticky-header')

    with ui.card().classes(
        'fixed top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 '
        'z-50 shadow-xl bg-white p-6 w-96 border border-gray-300 gap-0 hidden'
    ) as card:
        name_filter_card = card
        ui.label('Filter by Agent Name').classes('font-bold text-lg mb-2 text-center')
        ui.label('Filters will be retained on close of the app.').classes('text-red text-xs')
        ui.input('Search names...', on_change=lambda e: update_filter_query(e.value)).classes('w-full')
        name_checkbox_column = ui.column().classes('w-full h-64 overflow-y-scroll')  # for dynamic checkboxes
        with ui.row().classes('w-full justify-between'):
            ui.button('Clear Filter', on_click=lambda: (selected_names.clear(), update_table())).classes('mt-2')
            ui.button('Close', on_click=toggle_name_filter).props('flat').classes('mt-2 text-sm')

# Format timestamps
timestamp_fields = {
    'enteredStateOn': 'formatted_enteredStateOn',
    'lastLoginTime': 'formatted_lastLoginTime',
    'lastLogoffTime': 'formatted_lastLogoffTime',
}

if __name__ in {"__main__", "__mp_main__"}:
    load_selected_names()
    load_column_config()
    apply_column_order()
    ui.timer(interval=1, once=True, callback=lambda: asyncio.create_task(playwright_worker()))
    ui.timer(interval=1, callback=login_status_check)
    ui.timer(interval=1, callback=update_table)
    ui.add_head_html("""
    <style>
        .q-table__middle thead tr {
            position: sticky;
            top: 0;
            background-color: #ffffff;
            z-index: 10;
        }
         .nicegui-content {
            padding: 0 !important;
            margin: 0 !important;
        }
    </style>
    """)
    ui.run(show=False, reload=False)
    
