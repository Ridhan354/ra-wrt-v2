#!/bin/sh

set -u

SCRIPT_PATH="$0"
case "$SCRIPT_PATH" in
    /*) : ;;
    *) SCRIPT_PATH="$(pwd)/$SCRIPT_PATH" ;;
esac
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

INSTALL_DIR="/opt/ranet-bot"
BOT_SCRIPT="$INSTALL_DIR/ra-bot.py"
LOG_FILE="$INSTALL_DIR/ra-bot.log"
PID_FILE="$INSTALL_DIR/ra-bot.pid"
ID_FILE="$INSTALL_DIR/id-telegram.txt"
INIT_SCRIPT="/etc/init.d/ranet-bot"
RAW_BASE_URL="https://raw.githubusercontent.com/Ridhan354/ra-wrt-v2/main"
BOT_UPDATE_URL="$RAW_BASE_URL/ra-bot.py"
SETUP_NETBIRD_URL="$RAW_BASE_URL/setup-netbird.sh"
USB_WD_URL="$RAW_BASE_URL/usb-watchdog-setup.sh"
PING_TARGET="google.com"
PING_INTERVAL=5

wait_for_network() {
    echo "Menunggu koneksi internet..."
    attempt=0
    while true; do
        attempt=$((attempt + 1))
        if command_exists ping; then
            if ping -c1 -W3 "$PING_TARGET" >/dev/null 2>&1; then
                echo "Koneksi internet tersedia."
                return 0
            fi
        elif command_exists wget; then
            if wget -q --spider "https://$PING_TARGET" >/dev/null 2>&1; then
                echo "Koneksi internet tersedia."
                return 0
            fi
        elif command_exists curl; then
            if curl -fsI "https://$PING_TARGET" >/dev/null 2>&1; then
                echo "Koneksi internet tersedia."
                return 0
            fi
        else
            echo "[PERINGATAN] Tidak ada utilitas pengecekan internet (ping/wget/curl)." >&2
            return 1
        fi
        printf '  Menunggu koneksi (percobaan %s)...\n' "$attempt"
        sleep "$PING_INTERVAL"
    done
}

# Busybox ash doesn't support functions with hyphen names

print_header() {
    cat <<'TXT'
=======================================
 RANet Bot — Installer & Manager
=======================================
TXT
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "[ERR] Jalankan script ini sebagai root." >&2
        return 1
    fi
    return 0
}

pause() {
    printf '\nTekan Enter untuk kembali ke menu...'
    # shellcheck disable=SC2034
    read _unused || true
}

run_cmd() {
    cmd="$1"
    echo
    echo "==> $cmd"
    if ! sh -c "$cmd"; then
        echo "[PERINGATAN] Perintah gagal: $cmd" >&2
        return 1
    fi
    return 0
}

ensure_directory() {
    dir="$1"
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
    fi
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

download_file() {
    url="$1"
    dest="$2"
    mode=${3:-755}

    dest_dir="$(dirname "$dest")"
    [ -d "$dest_dir" ] || mkdir -p "$dest_dir"

    tmp_file="${dest}.tmp.$$"

    if command_exists curl; then
        if curl -fsSL "$url" -o "$tmp_file"; then
            mv "$tmp_file" "$dest"
            chmod $mode "$dest"
            return 0
        fi
        rm -f "$tmp_file"
    fi

    if command_exists wget; then
        if wget -q -O "$tmp_file" "$url"; then
            mv "$tmp_file" "$dest"
            chmod $mode "$dest"
            return 0
        fi
        rm -f "$tmp_file"
    fi

    echo "[ERR] Gagal mengunduh $url. Pastikan curl atau wget tersedia." >&2
    return 1
}

install_python_requirements() {
    echo "Memeriksa dependensi Python..."
    if command_exists python3; then
        run_cmd "python3 -c 'import sys; print(sys.version)'" || true
    else
        echo "[INFO] python3 belum terpasang. Akan mencoba menginstal lewat opkg." >&2
    fi

    pip_available=0
    if command_exists pip3; then
        pip_available=1
        run_cmd "pip3 install --break-system-packages 'python-telegram-bot[job-queue]>=20,<21'" || true
    else
        echo "[INFO] pip3 tidak ditemukan; tidak bisa memasang modul python-telegram-bot." >&2
    fi

    if command_exists opkg; then
        run_cmd "opkg update" || true
        run_cmd "opkg install python3 python3-pip python3-light python3-logging python3-asyncio \
    python3-sqlite3 python3-openssl python3-codecs python3-xml \
    ca-certificates libustream-mbedtls" || true
    else
        echo "[INFO] opkg tidak ditemukan. Lewati instalasi paket opkg." >&2
    fi

    if [ "$pip_available" -eq 1 ]; then
        run_cmd "pip3 install --upgrade pip" || true
        run_cmd "pip3 install 'python-telegram-bot[http2]==20.7'" || true
    fi
}

write_credentials() {
    ensure_directory "$INSTALL_DIR"
    printf '\nMasukkan BOT TOKEN Telegram: '
    read token || token=""
    printf 'Masukkan Chat ID (boleh lebih dari satu, pisahkan dengan koma/spasi): '
    read chat_ids || chat_ids=""

    {
        echo "TOKEN=$token"
        echo "CHAT_ID=$chat_ids"
    } >"$ID_FILE"
    chmod 600 "$ID_FILE"
    echo "Credential disimpan di $ID_FILE"
}

maybe_copy_optional_scripts() {
    printf '\nUnduh setup-netbird.sh ke %s? [y/N]: ' "$INSTALL_DIR"
    read answer || answer=""
    case "$answer" in
        y|Y)
            if download_file "$SETUP_NETBIRD_URL" "$INSTALL_DIR/setup-netbird.sh" 755; then
                echo "setup-netbird.sh diunduh."
            fi
            ;;
        *)
            echo "Lewati pengunduhan setup-netbird.sh."
            ;;
    esac

    printf 'Unduh usb-watchdog-setup.sh ke %s? [y/N]: ' "$INSTALL_DIR"
    read answer || answer=""
    case "$answer" in
        y|Y)
            if download_file "$USB_WD_URL" "$INSTALL_DIR/usb-watchdog-setup.sh" 755; then
                echo "usb-watchdog-setup.sh diunduh."
            fi
            ;;
        *)
            echo "Lewati pengunduhan usb-watchdog-setup.sh."
            ;;
    esac
}

install_bot() {
    require_root || return 1
    ensure_directory "$INSTALL_DIR"

    install_python_requirements

    if download_file "$BOT_UPDATE_URL" "$BOT_SCRIPT" 755; then
        echo "ra-bot.py diunduh ke $BOT_SCRIPT"
    else
        echo "[ERR] Instalasi dibatalkan karena gagal mengunduh ra-bot.py." >&2
        return 1
    fi

    maybe_copy_optional_scripts

    write_credentials

    echo "\nInstalasi selesai. Anda dapat menjalankan bot melalui menu Start bot."
}

stop_bot_process() {
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            if kill "$pid" 2>/dev/null; then
                echo "Proses bot ($pid) dihentikan."
            fi
        fi
        rm -f "$PID_FILE"
    fi
}

start_bot() {
    require_root || return 1
    ensure_directory "$INSTALL_DIR"
    if [ ! -f "$BOT_SCRIPT" ]; then
        echo "[ERR] $BOT_SCRIPT tidak ditemukan. Jalankan opsi Install terlebih dahulu." >&2
        return 1
    fi
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "Bot sudah berjalan dengan PID $pid."
            return 0
        fi
    fi
    if ! command_exists python3; then
        echo "[ERR] python3 tidak ditemukan." >&2
        return 1
    fi
    if ! wait_for_network; then
        echo "[PERINGATAN] Gagal memastikan koneksi internet. Mencoba menjalankan bot tetap." >&2
    fi
    ensure_directory "$INSTALL_DIR"
    cd "$INSTALL_DIR" || return 1
    if nohup python3 "$BOT_SCRIPT" >>"$LOG_FILE" 2>&1 & then
        bot_pid=$!
        echo "$bot_pid" >"$PID_FILE"
        echo "Bot dijalankan dengan PID $bot_pid (log: $LOG_FILE)."
    else
        echo "[ERR] Gagal menjalankan bot." >&2
        return 1
    fi
}

stop_bot() {
    require_root || return 1
    stop_bot_process
}

status_bot() {
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "Bot sedang berjalan (PID $pid)."
            return
        fi
    fi
    echo "Bot tidak berjalan."
}

restart_bot() {
    require_root || return 1
    stop_bot_process
    start_bot
}

ensure_init_script() {
    if [ -f "$INIT_SCRIPT" ]; then
        return 0
    fi

    if [ ! -d "$(dirname "$INIT_SCRIPT")" ]; then
        echo "[ERR] Direktori init.d tidak ditemukan." >&2
        return 1
    fi

    cat <<'INIT' >"$INIT_SCRIPT"
#!/bin/sh /etc/rc.common

START=95
STOP=5

INSTALL_DIR="/opt/ranet-bot"
BOT_SCRIPT="$INSTALL_DIR/ra-bot.py"
PID_FILE="$INSTALL_DIR/ra-bot.pid"
LOG_FILE="$INSTALL_DIR/ra-bot.log"
PING_TARGET="api.telegram.org"
PING_INTERVAL=5

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

wait_for_network() {
    echo "Menunggu koneksi internet..."
    attempt=0
    while true; do
        attempt=$((attempt + 1))
        if command_exists ping; then
            if ping -c1 -W3 "$PING_TARGET" >/dev/null 2>&1; then
                echo "Koneksi internet tersedia."
                return 0
            fi
        elif command_exists wget; then
            if wget -q --spider "https://$PING_TARGET" >/dev/null 2>&1; then
                echo "Koneksi internet tersedia."
                return 0
            fi
        elif command_exists curl; then
            if curl -fsI "https://$PING_TARGET" >/dev/null 2>&1; then
                echo "Koneksi internet tersedia."
                return 0
            fi
        else
            echo "Tidak ada utilitas pengecekan koneksi." >&2
            return 1
        fi
        printf '  Menunggu koneksi (percobaan %s)...\n' "$attempt"
        sleep "$PING_INTERVAL"
    done
}

start_service() {
    [ -f "$BOT_SCRIPT" ] || return 1
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    wait_for_network || echo "Tidak dapat memastikan koneksi internet." >&2
    nohup python3 "$BOT_SCRIPT" >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
}

stop_service() {
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
    fi
}
INIT
    chmod 755 "$INIT_SCRIPT"
    echo "Init script dibuat di $INIT_SCRIPT"
}

enable_boot() {
    require_root || return 1
    ensure_init_script || return 1
    if [ -x "$INIT_SCRIPT" ]; then
        "$INIT_SCRIPT" enable || true
        echo "Bot diaktifkan agar berjalan saat boot."
    else
        echo "[ERR] Init script tidak tersedia atau tidak dapat dieksekusi." >&2
        return 1
    fi
}

disable_boot() {
    require_root || return 1
    if [ -x "$INIT_SCRIPT" ]; then
        "$INIT_SCRIPT" disable || true
        echo "Bot dinonaktifkan dari auto-start."
    else
        echo "Init script tidak ditemukan."
    fi
}

update_bot() {
    require_root || return 1
    ensure_directory "$INSTALL_DIR"
    if download_file "$BOT_UPDATE_URL" "$BOT_SCRIPT" 755; then
        echo "Bot diperbarui dari GitHub."
    else
        return 1
    fi
}

tail_log() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "Log belum ada. Jalankan bot terlebih dahulu."
        return
    fi
    echo "Menampilkan log secara live. Tekan Ctrl+C untuk kembali."
    tail -f "$LOG_FILE"
}

reconfigure_credentials() {
    if [ ! -f "$ID_FILE" ]; then
        echo "File credential belum ada. Menulis baru." >&2
    fi
    write_credentials
}

uninstall_bot() {
    require_root || return 1
    stop_bot_process
    if [ -x "$INIT_SCRIPT" ]; then
        "$INIT_SCRIPT" disable || true
        rm -f "$INIT_SCRIPT"
        echo "Init script $INIT_SCRIPT dihapus."
    fi
    rm -f "$BOT_SCRIPT" "$LOG_FILE" "$PID_FILE"
    echo "File bot dihapus."
    printf 'Hapus file credential %s? [y/N]: ' "$ID_FILE"
    read answer || answer=""
    case "$answer" in
        y|Y)
            rm -f "$ID_FILE"
            echo "Credential dihapus."
            ;;
        *)
            echo "Credential dibiarkan."
            ;;
    esac
    printf 'Hapus direktori %s? [y/N]: ' "$INSTALL_DIR"
    read answer || answer=""
    case "$answer" in
        y|Y)
            rm -rf "$INSTALL_DIR"
            echo "Direktori $INSTALL_DIR dihapus."
            ;;
        *)
            echo "Direktori $INSTALL_DIR tidak dihapus."
            ;;
    esac
}

main_menu() {
    while true; do
        print_header
        cat <<'MENU'
 1) Install
 2) Uninstall
 3) Start bot
 4) Stop bot
 5) Restart bot
 6) Status
 7) Enable on boot
 8) Disable on boot
 9) Update (pull dari GitHub)
10) Tail log (live)
11) Reconfigure (TOKEN/Chat ID)
12) Keluar
MENU
        printf 'Pilih [1-12]: '
        read choice || exit 0
        case "$choice" in
            1) install_bot; pause ;;
            2) uninstall_bot; pause ;;
            3) start_bot; pause ;;
            4) stop_bot; pause ;;
            5) restart_bot; pause ;;
            6) status_bot; pause ;;
            7) enable_boot; pause ;;
            8) disable_boot; pause ;;
            9) update_bot; pause ;;
            10) tail_log ;;
            11) reconfigure_credentials; pause ;;
            12) echo "Keluar."; exit 0 ;;
            *) echo "Pilihan tidak valid."; pause ;;
        esac
    done
}

main_menu
