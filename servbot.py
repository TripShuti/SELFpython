import os
import subprocess
import psutil
import telebot
import threading
import time
import logging
from telebot import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "--------"
ADMIN_ID = ---------

bot = telebot.TeleBot(TOKEN)

alert_flags = {
    "disk_root": False,
    "disk_ssd": False,
    "cpu_temp": False,
    "reboot_required": False,
}
prev_container_states = {}
summary_hour = 17
summary_minute = 0

SSH_ALERT_THRESHOLD = 3  
last_ssh_check = time.time()


def is_admin(message):
    return message.from_user.id == ADMIN_ID


def get_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Статус", "Диски", "Сервіси")
    kb.add("Оновлення", "Ребут", "Час зведення")
    return kb


def safe_truncate_html(text, limit=4096, from_tail=False):
    """
    Обрізає текст по межах рядків, а не по довільному символу,
    щоб не поламати відкриті/закриті HTML-теги (<code>, <pre> тощо).
    """
    if len(text) <= limit:
        return text

    lines = text.split('\n')
    result = []
    total = 0
    reserve = 10  

    if from_tail:
        for line in reversed(lines):
            if total + len(line) + 1 > limit - reserve:
                break
            result.append(line)
            total += len(line) + 1
        result.reverse()
        return "...\n" + "\n".join(result) if result else text[-(limit - reserve):] + "..."
    else:
        for line in lines:
            if total + len(line) + 1 > limit - reserve:
                break
            result.append(line)
            total += len(line) + 1
        return "\n".join(result) + "\n..." if result else text[:limit - reserve] + "..."


def get_container_states():
    try:
        out = subprocess.check_output(
            "docker ps -a --format '{{.Names}}|{{.State}}'",
            shell=True, text=True
        ).strip()
        states = {}
        for line in out.split('\n'):
            if '|' in line:
                name, state = line.split('|', 1)
                states[name] = state
        return states
    except Exception as e:
        logger.error(f"get_container_states error: {e}")
        return {}


def build_services_keyboard():
    states = get_container_states()
    inline_kb = types.InlineKeyboardMarkup()
    for name, state in states.items():
        emoji = "🟢" if state == "running" else "🔴"
        inline_kb.add(types.InlineKeyboardButton(f"{emoji} {name}", callback_data=f"dock_menu_{name}"))
    return inline_kb


def get_power_events_today():
    try:
        raw = subprocess.check_output(
            "journalctl --list-boots --no-pager",
            shell=True, text=True
        ).strip()

        lines = [l for l in raw.split('\n') if l.strip()]
        today = time.strftime("%Y-%m-%d")
        events = []

        sessions = []
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            try:
                boot_num = int(parts[0])
                start_str = f"{parts[3]} {parts[4]}"
                end_str = f"{parts[7]} {parts[8]}"
                sessions.append((boot_num, start_str, end_str))
            except (ValueError, IndexError):
                continue

        for i, (boot_num, start_str, end_str) in enumerate(sessions):
            if i == 0:
                continue
            prev_end = sessions[i - 1][2]

            if today not in prev_end or today not in start_str:
                continue

            try:
                prev_end_ts = time.mktime(time.strptime(prev_end, "%Y-%m-%d %H:%M:%S"))
                start_ts = time.mktime(time.strptime(start_str, "%Y-%m-%d %H:%M:%S"))
                gap = start_ts - prev_end_ts
                if gap > 300:
                    off_time = time.strftime("%H:%M:%S", time.localtime(prev_end_ts))
                    on_time = time.strftime("%H:%M:%S", time.localtime(start_ts))
                    duration_min = int(gap // 60)
                    events.append(
                        f"<code>{off_time}</code> → <code>{on_time}</code>  ({duration_min} хв)"
                    )
            except ValueError:
                pass

        if not events:
            return "<b>Живлення:</b> відключень не було"

        return "<b>Живлення — відключення за добу:</b>\n" + "\n".join(events)

    except Exception as e:
        logger.error(f"get_power_events_today error: {e}")
        return "<b>Живлення:</b> помилка читання логів"


def check_reboot_required():
    """Перевіряє прапорець /var/run/reboot-required, який ставить apt
    після встановлення пакетів, що вимагають перезавантаження (ядро, libc, systemd тощо)."""
    if os.path.exists('/var/run/reboot-required'):
        pkgs_file = '/var/run/reboot-required.pkgs'
        pkgs = []
        if os.path.exists(pkgs_file):
            try:
                with open(pkgs_file) as f:
                    pkgs = [l.strip() for l in f if l.strip()]
            except Exception:
                pass
        text = "🔄 <b>Серверу потрібне перезавантаження</b>"
        if pkgs:
            text += "\nПричина: " + ", ".join(f"<code>{p}</code>" for p in pkgs)
        return text
    return None


def check_failed_ssh():
    """Перевіряє журнал systemd на невдалі спроби SSH-входу з моменту останньої перевірки."""
    global last_ssh_check
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_ssh_check))
    now = time.time()
    try:
        out = subprocess.check_output(
            f"journalctl -u ssh -u sshd --since '{since}' --no-pager 2>/dev/null "
            f"| grep -iE 'Failed password|Invalid user' || true",
            shell=True, text=True
        ).strip()
    except Exception as e:
        logger.error(f"check_failed_ssh error: {e}")
        last_ssh_check = now
        return None

    last_ssh_check = now

    if not out:
        return None

    lines = [l for l in out.split('\n') if l.strip()]
    if len(lines) < SSH_ALERT_THRESHOLD:
        return None

    ips = {}
    for line in lines:
        parts = line.split()
        if "from" in parts:
            idx = parts.index("from")
            if idx + 1 < len(parts):
                ip = parts[idx + 1]
                ips[ip] = ips.get(ip, 0) + 1

    text = f"🔐 <b>Невдалі спроби SSH-входу: {len(lines)}</b>"
    if ips:
        top = sorted(ips.items(), key=lambda x: -x[1])[:10]
        text += "\n" + "\n".join(f"<code>{ip}</code>: {cnt}" for ip, cnt in top)
    return text


def alert_monitor():
    global prev_container_states

    while True:
        try:
            disk_root = psutil.disk_usage('/')
            if disk_root.percent > 90:
                if not alert_flags["disk_root"]:
                    bot.send_message(ADMIN_ID, f"⚠️ <b>Диск / заповнений на {disk_root.percent}%</b>", parse_mode="HTML")
                    alert_flags["disk_root"] = True
            else:
                alert_flags["disk_root"] = False
        except Exception as e:
            logger.error(f"disk_root check error: {e}")

        try:
            disk_ssd = psutil.disk_usage('/mnt/ssd')
            if disk_ssd.percent > 90:
                if not alert_flags["disk_ssd"]:
                    bot.send_message(ADMIN_ID, f"⚠️ <b>Диск /mnt/ssd заповнений на {disk_ssd.percent}%</b>", parse_mode="HTML")
                    alert_flags["disk_ssd"] = True
            else:
                alert_flags["disk_ssd"] = False
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"disk_ssd check error: {e}")

        try:
            temps = psutil.sensors_temperatures()
            if "coretemp" in temps and temps["coretemp"][0].current > 80:
                if not alert_flags["cpu_temp"]:
                    bot.send_message(ADMIN_ID, f"🔥 <b>Процесор перегрівся: {temps['coretemp'][0].current}°C</b>", parse_mode="HTML")
                    alert_flags["cpu_temp"] = True
            else:
                alert_flags["cpu_temp"] = False
        except Exception as e:
            logger.error(f"cpu_temp check error: {e}")

        try:
            reboot_msg = check_reboot_required()
            if reboot_msg:
                if not alert_flags["reboot_required"]:
                    inline_kb = types.InlineKeyboardMarkup()
                    inline_kb.add(types.InlineKeyboardButton("🔄 Перезавантажити зараз", callback_data="confirm_reboot"))
                    bot.send_message(ADMIN_ID, reboot_msg, parse_mode="HTML", reply_markup=inline_kb)
                    alert_flags["reboot_required"] = True
            else:
                alert_flags["reboot_required"] = False
        except Exception as e:
            logger.error(f"reboot_required check error: {e}")

        try:
            ssh_msg = check_failed_ssh()
            if ssh_msg:
                bot.send_message(ADMIN_ID, ssh_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"ssh alert error: {e}")

        try:
            current_states = get_container_states()
            for name, state in current_states.items():
                prev_state = prev_container_states.get(name)
                if prev_state == "running" and state != "running":
                    try:
                        inspect = subprocess.check_output(
                            ["docker", "inspect", "--format",
                             "{{.State.ExitCode}} {{.State.OOMKilled}} {{.State.Error}}", name],
                            text=True
                        ).strip()
                        parts = inspect.split(' ', 2)
                        exit_code = parts[0] if len(parts) > 0 else '?'
                        oom = parts[1] if len(parts) > 1 else 'false'
                        error = parts[2] if len(parts) > 2 else ''

                        reason_lines = [f"Код виходу: <code>{exit_code}</code>"]
                        if oom == 'true':
                            reason_lines.append("Причина: <b>OOM</b> (не вистачило пам'яті)")
                        if error and error != '<no value>':
                            reason_lines.append(f"Помилка: <code>{error}</code>")

                        reason_str = "\n".join(reason_lines)
                        bot.send_message(ADMIN_ID, f"🔴 <b>Контейнер впав: {name}</b>\n{reason_str}", parse_mode="HTML")
                    except Exception:
                        bot.send_message(ADMIN_ID, f"🔴 <b>Контейнер впав: {name}</b>", parse_mode="HTML")
                elif prev_state and prev_state != "running" and state == "running":
                    bot.send_message(ADMIN_ID, f"🟢 <b>Контейнер запустився: {name}</b>", parse_mode="HTML")
            prev_container_states = current_states
        except Exception as e:
            logger.error(f"container state check error: {e}")

        time.sleep(300)


def daily_summary():
    last_sent_day = None

    while True:
        try:
            now = time.localtime()
            if now.tm_hour == summary_hour and now.tm_min == summary_minute and now.tm_yday != last_sent_day:
                last_sent_day = now.tm_yday
                try:
                    uptime = subprocess.check_output("uptime -p", shell=True, text=True).strip()
                    cpu = psutil.cpu_percent(1)
                    ram = psutil.virtual_memory().percent
                    temps = psutil.sensors_temperatures()
                    temp_str = f"{temps['coretemp'][0].current}°C" if "coretemp" in temps else "N/A"

                    states = get_container_states()
                    running = sum(1 for s in states.values() if s == "running")
                    total = len(states)

                    stopped = [n for n, s in states.items() if s != "running"]
                    stopped_str = ""
                    if stopped:
                        stopped_str = "\n🔴 Зупинені: " + ", ".join(f"<code>{n}</code>" for n in stopped)

                    try:
                        last_boot = subprocess.check_output("who -b", shell=True, text=True).strip()
                    except Exception:
                        last_boot = "N/A"

                    power_info = get_power_events_today()

                    reboot_msg = check_reboot_required()
                    reboot_line = f"\n\n{reboot_msg}" if reboot_msg else ""

                    bot.send_message(
                        ADMIN_ID,
                        f"<b>📋 Щоденне зведення</b>\n\n"
                        f"Uptime: <code>{uptime}</code>\n"
                        f"CPU: <code>{cpu}%</code>\n"
                        f"RAM: <code>{ram}%</code>\n"
                        f"Temp: <code>{temp_str}</code>\n"
                        f"Сервіси: <b>{running}/{total}</b> працює{stopped_str}\n"
                        f"Останнє завантаження: <code>{last_boot}</code>\n\n"
                        f"{power_info}"
                        f"{reboot_line}",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"daily_summary send error: {e}")
        except Exception as e:
            logger.error(f"daily_summary loop error: {e}")

        time.sleep(30)


@bot.message_handler(commands=['start'])
def start(message):
    if is_admin(message):
        bot.send_message(message.chat.id, "<b>Сервер онлайн.</b>", parse_mode="HTML", reply_markup=get_kb())


@bot.message_handler(func=is_admin)
def handle_text(message):
    if message.text == "Статус":
        try:
            uptime = subprocess.check_output("uptime -p", shell=True, text=True).strip()
            cpu = psutil.cpu_percent(1)
            ram = psutil.virtual_memory().percent

            temp_str = "N/A"
            temps = psutil.sensors_temperatures()
            if "coretemp" in temps:
                temp_str = f"{temps['coretemp'][0].current}°C"

            power_info = get_power_events_today()

            bot.send_message(
                message.chat.id,
                f"<b>📊 Статус сервера</b>\n\n"
                f"Uptime: <code>{uptime}</code>\n"
                f"CPU: <code>{cpu}%</code>\n"
                f"RAM: <code>{ram}%</code>\n"
                f"Temp: <code>{temp_str}</code>\n\n"
                f"{power_info}",
                parse_mode="HTML"
            )
        except Exception as e:
            bot.send_message(message.chat.id, f"Помилка: {e}")

    elif message.text == "Диски":
        def fmt(b):
            return f"{b / (1024**3):.1f}G"

        try:
            rt = psutil.disk_usage('/')
            text = f"<b>📁 Системний (/)</b>\nВжито: <code>{fmt(rt.used)}</code> з <code>{fmt(rt.total)}</code> ({rt.percent}%)"
            try:
                sd = psutil.disk_usage('/mnt/ssd')
                text += f"\n\n<b>📁 Медіа (/mnt/ssd)</b>\nВжито: <code>{fmt(sd.used)}</code> з <code>{fmt(sd.total)}</code> ({sd.percent}%)"
            except FileNotFoundError:
                text += "\n\n<b>📁 /mnt/ssd:</b> не змонтовано"
        except Exception as e:
            text = f"Помилка отримання даних: {e}"

        bot.send_message(message.chat.id, text, parse_mode="HTML")

    elif message.text == "Сервіси":
        inline_kb = build_services_keyboard()
        if inline_kb.keyboard:
            bot.send_message(message.chat.id, "<b>Список усіх сервісів</b>", parse_mode="HTML", reply_markup=inline_kb)
        else:
            bot.send_message(message.chat.id, "Контейнерів не знайдено.")

    elif message.text == "Оновлення":
        bot.send_message(message.chat.id, "Перевіряю...")
        try:
            subprocess.run("sudo /usr/bin/apt-get update -qq", shell=True)
            out = subprocess.check_output(
                "LC_ALL=C /usr/bin/apt-get -s upgrade | grep -c ^Inst || true",
                shell=True, text=True
            )
            count = int(out.strip()) if out.strip().isdigit() else 0

            if count > 0:
                inline_kb = types.InlineKeyboardMarkup()
                inline_kb.row(
                    types.InlineKeyboardButton("Що оновити", callback_data="apt_list"),
                    types.InlineKeyboardButton("Оновити все", callback_data="apt_upgrade")
                )
                bot.send_message(message.chat.id, f"<b>Доступно оновлень: {count}</b>", parse_mode="HTML", reply_markup=inline_kb)
            else:
                bot.send_message(message.chat.id, "✅ Система оновлена.")
        except Exception as e:
            bot.send_message(message.chat.id, f"Помилка перевірки оновлень: {e}")

    elif message.text == "Ребут":
        inline_kb = types.InlineKeyboardMarkup()
        inline_kb.row(
            types.InlineKeyboardButton("✅ Так, перезавантажити", callback_data="confirm_reboot"),
            types.InlineKeyboardButton("❌ Скасувати", callback_data="cancel_reboot")
        )
        bot.send_message(message.chat.id, "⚠️ <b>Точно перезавантажити сервер?</b>", parse_mode="HTML", reply_markup=inline_kb)

    elif message.text == "Час зведення":
        bot.send_message(
            message.chat.id,
            f"Поточний час щоденного зведення: <code>{summary_hour:02d}:{summary_minute:02d}</code>\n\n"
            f"Надішліть новий час у форматі ГГ:ХХ\nНаприклад: <code>09:00</code> або <code>21:30</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(message, set_summary_time)


def set_summary_time(message):
    global summary_hour, summary_minute
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.strip().split(':')
        if len(parts) != 2:
            raise ValueError
        h = int(parts[0])
        m = int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        summary_hour = h
        summary_minute = m
        bot.send_message(message.chat.id, f"✅ Час зведення встановлено: <code>{summary_hour:02d}:{summary_minute:02d}</code>", parse_mode="HTML")
    except ValueError:
        bot.send_message(message.chat.id, "Невірний формат. Введіть час у форматі ГГ:ХХ, наприклад 17:00")


@bot.callback_query_handler(func=lambda call: call.data == "confirm_reboot")
def confirm_reboot(call):
    if call.from_user.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id, "Перезавантажую...")
    try:
        bot.edit_message_text("🔄 Відправляю в ребут...", call.message.chat.id, call.message.message_id)
    except Exception:
        bot.send_message(call.message.chat.id, "🔄 Відправляю в ребут...")
    os.system("sudo /usr/sbin/reboot")


@bot.callback_query_handler(func=lambda call: call.data == "cancel_reboot")
def cancel_reboot(call):
    if call.from_user.id != ADMIN_ID:
        return
    bot.answer_callback_query(call.id, "Скасовано")
    try:
        bot.edit_message_text("❌ Перезавантаження скасовано.", call.message.chat.id, call.message.message_id)
    except Exception:
        bot.send_message(call.message.chat.id, "❌ Перезавантаження скасовано.")


@bot.callback_query_handler(func=lambda call: call.data == "apt_list")
def apt_list(call):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        out = subprocess.check_output(
            "LC_ALL=C /usr/bin/apt-get -s upgrade | grep ^Inst",
            shell=True, text=True
        ).strip()
        lines = out.split('\n')
        packages = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                pkg = parts[1]
                old = next((p.strip('[]') for p in parts if p.startswith('[')), '?')
                new = next((p for p in parts[2:] if not p.startswith('[') and not p.startswith('(') and not p.startswith(')')), '?')
                packages.append(f"<code>{pkg}</code>: {old} → {new}")

        text = "<b>📦 Доступні оновлення</b>\n\n" + "\n".join(packages)
        text = safe_truncate_html(text, from_tail=False)
    except Exception as e:
        text = f"Помилка отримання списку: {e}"

    inline_kb = types.InlineKeyboardMarkup()
    inline_kb.add(types.InlineKeyboardButton("Оновити все", callback_data="apt_upgrade"))
    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=inline_kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("dock_menu_"))
def dock_menu(call):
    if call.from_user.id != ADMIN_ID:
        return
    name = call.data.replace("dock_menu_", "")
    try:
        state = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            text=True
        ).strip()
        is_running = state == "true"
    except Exception:
        is_running = False

    inline_kb = types.InlineKeyboardMarkup()
    if is_running:
        inline_kb.row(
            types.InlineKeyboardButton("🔄 Перезапуск", callback_data=f"dock_act_restart_{name}"),
            types.InlineKeyboardButton("⏹ Зупинити", callback_data=f"dock_act_stop_{name}")
        )
    else:
        inline_kb.add(types.InlineKeyboardButton("▶️ Запустити", callback_data=f"dock_act_start_{name}"))

    inline_kb.add(types.InlineKeyboardButton("📋 Логи", callback_data=f"dock_logs_{name}"))
    inline_kb.add(types.InlineKeyboardButton("« Назад до списку", callback_data="dock_back"))

    status_str = "🟢 Працює" if is_running else "🔴 Зупинено"
    bot.edit_message_text(
        f"<b>Сервіс:</b> <code>{name}</code>\n<b>Статус:</b> {status_str}\n\nОберіть дію:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=inline_kb
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("dock_logs_"))
def dock_logs(call):
    if call.from_user.id != ADMIN_ID:
        return
    name = call.data.replace("dock_logs_", "")
    try:
        logs = subprocess.check_output(
            ["docker", "logs", "--tail", "20", name],
            stderr=subprocess.STDOUT, text=True
        ).strip()
        if not logs:
            logs = "Логів немає."
        logs_escaped = logs.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = f"<b>📋 Логи {name} (останні 20 рядків)</b>\n\n<pre>{logs_escaped}</pre>"
        text = safe_truncate_html(text, from_tail=True)
    except Exception as e:
        text = f"Помилка отримання логів: {e}"

    inline_kb = types.InlineKeyboardMarkup()
    inline_kb.add(types.InlineKeyboardButton("« Назад", callback_data=f"dock_menu_{name}"))
    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=inline_kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("dock_act_"))
def dock_action(call):
    if call.from_user.id != ADMIN_ID:
        return
    data = call.data.replace("dock_act_", "")
    action, name = data.split("_", 1)

    bot.edit_message_text(
        f"⏳ Виконую <b>{action}</b> для <code>{name}</code>...",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML"
    )

    if action == "restart":
        cmd = ["docker", "restart", name]
    elif action == "stop":
        cmd = ["docker", "stop", name]
    elif action == "start":
        cmd = ["docker", "start", name]
    else:
        bot.send_message(call.message.chat.id, "Невідома команда.")
        return

    result = subprocess.run(cmd, capture_output=True, text=True)

    inline_kb = types.InlineKeyboardMarkup()
    inline_kb.add(types.InlineKeyboardButton("« До списку сервісів", callback_data="dock_back"))

    if result.returncode != 0:
        bot.send_message(call.message.chat.id, f"🔴 <b>Помилка {action} для {name}</b>\n<pre>{result.stderr[-500:]}</pre>", parse_mode="HTML", reply_markup=inline_kb)
    else:
        bot.send_message(call.message.chat.id, f"🟢 <b>Виконано: {action} для {name}</b>", parse_mode="HTML", reply_markup=inline_kb)


@bot.callback_query_handler(func=lambda call: call.data == "dock_back")
def dock_back(call):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        inline_kb = build_services_keyboard()
        bot.edit_message_text(
            "<b>Список усіх сервісів</b>",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=inline_kb
        )
    except Exception as e:
        bot.send_message(call.message.chat.id, f"Помилка: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "apt_upgrade")
def run_upgrade(call):
    if call.from_user.id != ADMIN_ID:
        return
    bot.edit_message_text("⏳ Оновлюю пакети, зачекайте...", call.message.chat.id, call.message.message_id)
    result = subprocess.run(
        "sudo /usr/bin/apt-get upgrade -y -qq",
        shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        bot.send_message(call.message.chat.id, f"🔴 <b>Помилка оновлення</b>\n<pre>{result.stderr[-500:]}</pre>", parse_mode="HTML")
    else:
        bot.send_message(call.message.chat.id, "✅ <b>Оновлення завершено.</b>", parse_mode="HTML")
        reboot_msg = check_reboot_required()
        if reboot_msg:
            alert_flags["reboot_required"] = True
            inline_kb = types.InlineKeyboardMarkup()
            inline_kb.add(types.InlineKeyboardButton("🔄 Перезавантажити зараз", callback_data="confirm_reboot"))
            bot.send_message(call.message.chat.id, reboot_msg, parse_mode="HTML", reply_markup=inline_kb)


if __name__ == "__main__":
    threading.Thread(target=alert_monitor, daemon=True).start()
    threading.Thread(target=daily_summary, daemon=True).start()
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            logger.error(f"polling crashed: {e}")
            time.sleep(15)