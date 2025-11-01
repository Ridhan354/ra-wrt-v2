#!/bin/sh
# USB Watchdog Setup Script — monitor network interface connectivity and reboot on repeated failures.

CONF_FILE="/etc/usb-watchdog.conf"
SERVICE_FILE="/usr/bin/usb-watchdog.sh"
PID_FILE="/var/run/usb-watchdog.pid"
RC_LOCAL="/etc/rc.local"

DEFAULT_INTERFACE="usb0"
DEFAULT_INTERVAL="15"
DEFAULT_MAX_ATTEMPTS="3"
DEFAULT_LOG_FILE="/var/log/usb-watchdog.log"
DEFAULT_LOGGING="yes"

PING_HOSTS="1.1.1.1 8.8.8.8"

if [ -t 1 ]; then
  CLR_OK="\033[1;32m"; CLR_WARN="\033[1;33m"; CLR_ERR="\033[1;31m"; CLR_INFO="\033[1;36m"; CLR_RESET="\033[0m"
else
  CLR_OK=""; CLR_WARN=""; CLR_ERR=""; CLR_INFO=""; CLR_RESET=""
fi

say()  { printf "%s\n" "$*"; }
ok()   { printf "%b[OK]%b %s\n" "$CLR_OK" "$CLR_RESET" "$*"; }
warn() { printf "%b[WARN]%b %s\n" "$CLR_WARN" "$CLR_RESET" "$*"; }
err()  { printf "%b[ERR]%b %s\n" "$CLR_ERR" "$CLR_RESET" "$*"; }
info() { printf "%b[INFO]%b %s\n" "$CLR_INFO" "$CLR_RESET" "$*"; }

require_root() {
  if [ "$(id -u 2>/dev/null || echo 1)" != "0" ]; then
    err "Script ini harus dijalankan sebagai root."
    exit 1
  fi
}

trim() {
  printf "%s" "$1" | sed -e 's/^\s*//' -e 's/\s*$//'
}

load_config() {
  CONF_INTERFACE=""
  CONF_INTERVAL=""
  CONF_MAX_ATTEMPTS=""
  CONF_LOG_FILE=""
  CONF_LOGGING=""
  if [ -f "$CONF_FILE" ]; then
    while IFS='=' read -r key value; do
      key="$(printf "%s" "$key" | tr '[:lower:]' '[:upper:]' | sed -e 's/[^A-Z0-9_].*//')"
      value="$(trim "$value")"
      value="$(printf "%s" "$value" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
      case "$key" in
        INTERFACE) CONF_INTERFACE="$value" ;;
        CHECK_INTERVAL) CONF_INTERVAL="$value" ;;
        MAX_ATTEMPTS) CONF_MAX_ATTEMPTS="$value" ;;
        LOG_FILE) CONF_LOG_FILE="$value" ;;
        LOGGING_ENABLED) CONF_LOGGING="$value" ;;
      esac
    done < "$CONF_FILE"
  fi
  [ -n "$CONF_INTERFACE" ] || CONF_INTERFACE="$DEFAULT_INTERFACE"
  [ -n "$CONF_INTERVAL" ] || CONF_INTERVAL="$DEFAULT_INTERVAL"
  [ -n "$CONF_MAX_ATTEMPTS" ] || CONF_MAX_ATTEMPTS="$DEFAULT_MAX_ATTEMPTS"
  [ -n "$CONF_LOG_FILE" ] || CONF_LOG_FILE="$DEFAULT_LOG_FILE"
  [ -n "$CONF_LOGGING" ] || CONF_LOGGING="$DEFAULT_LOGGING"
}

print_config() {
  load_config
  say "===== USB Watchdog Configuration ====="
  say "Interface        : $CONF_INTERFACE"
  say "Check Interval   : $CONF_INTERVAL detik"
  say "Max Attempts     : $CONF_MAX_ATTEMPTS"
  say "Log File         : $CONF_LOG_FILE"
  say "Logging Enabled  : $CONF_LOGGING"
  say "Config Path      : $CONF_FILE"
  print_log_status "$CONF_LOG_FILE" "$CONF_LOGGING" "$CONF_INTERFACE"
}

print_log_status() {
  log_file="$1"
  logging="$2"
  iface_name="$3"

  if [ "$(printf "%s" "$logging" | tr '[:upper:]' '[:lower:]')" = "no" ]; then
    warn "USB status        : Logging dinonaktifkan"
    return
  fi

  if [ -z "$log_file" ]; then
    warn "USB status        : Lokasi log belum dikonfigurasi"
    return
  fi

  if [ ! -f "$log_file" ]; then
    warn "USB status        : Log belum dibuat ($log_file)"
    return
  fi

  last_line="$(tail -n 1 "$log_file" 2>/dev/null)"
  if [ -z "$last_line" ]; then
    warn "USB status        : Log masih kosong ($log_file)"
    return
  fi

  status_label="UNKNOWN"
  case "$last_line" in
    *"normal"*|*"Normal"*) status_label="ONLINE" ;;
    *"Deteksi kegagalan"*|*"gagal"*|*"tidak"*|*"down"*|*"reboot"*|*"Reboot"*) status_label="OFFLINE" ;;
  esac

  case "$status_label" in
    ONLINE)
      ok "USB status        : ONLINE (${iface_name:-n/a})"
      ;;
    OFFLINE)
      err "USB status        : OFFLINE (${iface_name:-n/a})"
      ;;
    *)
      warn "USB status        : UNKNOWN (${iface_name:-n/a})"
      ;;
  esac

  say "Log terakhir      : $last_line"
}

write_config() {
  require_root
  interface="$1"
  interval="$2"
  attempts="$3"
  log_file="$4"
  logging="$5"
  cat > "$CONF_FILE" <<EOCFG
# USB Watchdog configuration
INTERFACE=$interface
CHECK_INTERVAL=$interval
MAX_ATTEMPTS=$attempts
LOG_FILE=$log_file
LOGGING_ENABLED=$logging
EOCFG
  chmod 600 "$CONF_FILE" 2>/dev/null || true
  ok "Konfigurasi tersimpan: $CONF_FILE"
}

validate_uint() {
  val="$1"; min="$2"; def="$3"
  case "$val" in
    ''|*[!0-9]*) val="$def" ;;
  esac
  if [ "$val" -lt "$min" ] 2>/dev/null; then
    val="$min"
  fi
  printf "%s" "$val"
}

normalize_logging() {
  case "$(printf "%s" "$1" | tr '[:upper:]' '[:lower:]')" in
    yes|y|true|1|enable|enabled) printf "yes" ;;
    no|n|false|0|disable|disabled) printf "no" ;;
    *) printf "%s" "$DEFAULT_LOGGING" ;;
  esac
}

list_interfaces() {
  if command -v ip >/dev/null 2>&1; then
    ip -o link show | awk -F': ' '{print $2}'
    return 0
  fi
  if command -v ifconfig >/dev/null 2>&1; then
    ifconfig -a | sed -n 's/^[ \t]*\([^: ]*\).*/\1/p'
    return 0
  fi
  if [ -d /sys/class/net ]; then
    ls /sys/class/net
    return 0
  fi
  err "Tidak bisa membaca daftar interface (butuh ip atau ifconfig)."
  return 1
}

ensure_service_script() {
  require_root
  cat > "$SERVICE_FILE" <<'EOSVC'
#!/bin/sh
# USB Watchdog runtime service — auto reboot when interface loses connectivity repeatedly.

CONF_FILE="/etc/usb-watchdog.conf"
PID_FILE="/var/run/usb-watchdog.pid"
LOG_TAG="usb-watchdog"
DEFAULT_INTERFACE="usb0"
DEFAULT_INTERVAL=15
DEFAULT_MAX_ATTEMPTS=3
DEFAULT_LOG_FILE="/var/log/usb-watchdog.log"
DEFAULT_LOGGING="yes"
PING_HOSTS="1.1.1.1 8.8.8.8"

log_msg() {
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  if command -v logger >/dev/null 2>&1; then
    logger -t "$LOG_TAG" -- "$*"
  fi
  if [ "$LOGGING_ENABLED" = "yes" ] && [ -n "$LOG_FILE" ]; then
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    printf "%s %s\n" "$ts" "$*" >> "$LOG_FILE" 2>/dev/null || true
  fi
}

load_conf() {
  IFACE=""
  CHECK_INTERVAL=""
  MAX_ATTEMPTS=""
  LOG_FILE=""
  LOGGING_ENABLED=""
  if [ -f "$CONF_FILE" ]; then
    while IFS='=' read -r key value; do
      case "$(printf "%s" "$key" | tr '[:lower:]' '[:upper:]')" in
        INTERFACE) IFACE="$(printf "%s" "$value" | sed -e 's/^\s*//' -e 's/\s*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")" ;;
        CHECK_INTERVAL) CHECK_INTERVAL="$(printf "%s" "$value" | tr -cd '0-9')" ;;
        MAX_ATTEMPTS) MAX_ATTEMPTS="$(printf "%s" "$value" | tr -cd '0-9')" ;;
        LOG_FILE) LOG_FILE="$(printf "%s" "$value" | sed -e 's/^\s*//' -e 's/\s*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")" ;;
        LOGGING_ENABLED) LOGGING_ENABLED="$(printf "%s" "$value" | tr '[:upper:]' '[:lower:]' | sed 's/\s//g')" ;;
      esac
    done < "$CONF_FILE"
  fi
  [ -n "$IFACE" ] || IFACE="${DEFAULT_INTERFACE:-usb0}"
  [ -n "$CHECK_INTERVAL" ] || CHECK_INTERVAL=$DEFAULT_INTERVAL
  [ "$CHECK_INTERVAL" -gt 2 ] 2>/dev/null || CHECK_INTERVAL=$DEFAULT_INTERVAL
  [ -n "$MAX_ATTEMPTS" ] || MAX_ATTEMPTS=$DEFAULT_MAX_ATTEMPTS
  [ "$MAX_ATTEMPTS" -gt 0 ] 2>/dev/null || MAX_ATTEMPTS=$DEFAULT_MAX_ATTEMPTS
  [ -n "$LOG_FILE" ] || LOG_FILE="$DEFAULT_LOG_FILE"
  case "$LOGGING_ENABLED" in
    yes|y|true|1) LOGGING_ENABLED="yes" ;;
    no|n|false|0) LOGGING_ENABLED="no" ;;
    *) LOGGING_ENABLED="$DEFAULT_LOGGING" ;;
  esac
}

check_interface() {
  iface="$1"
  if command -v ip >/dev/null 2>&1; then
    if ! ip link show dev "$iface" >/dev/null 2>&1; then
      log_msg "Interface $iface tidak ditemukan"
      return 1
    fi
    link_info="$(ip -o link show dev "$iface" 2>/dev/null)"
    link_ok="no"
    if printf "%s" "$link_info" | grep -Eq 'state (UP|UNKNOWN)'; then
      link_ok="yes"
    elif printf "%s" "$link_info" | grep -Eq '<[^>]*LOWER_UP'; then
      link_ok="yes"
    elif [ -r "/sys/class/net/$iface/operstate" ]; then
      read -r oper < "/sys/class/net/$iface/operstate"
      case "$oper" in
        up|unknown|dormant) link_ok="yes" ;;
      esac
    fi
    if [ "$link_ok" != "yes" ]; then
      if ip addr show dev "$iface" 2>/dev/null | grep -q "inet "; then
        link_ok="yes"
      fi
    fi
    if [ "$link_ok" != "yes" ]; then
      log_msg "Interface $iface belum aktif"
      return 1
    fi
  elif command -v ifconfig >/dev/null 2>&1; then
    if ! ifconfig "$iface" >/dev/null 2>&1; then
      log_msg "Interface $iface tidak ditemukan"
      return 1
    fi
    if ! ifconfig "$iface" | grep -q "RUNNING"; then
      log_msg "Interface $iface belum aktif"
      return 1
    fi
  elif [ ! -d "/sys/class/net/$iface" ]; then
    log_msg "Interface $iface tidak tersedia"
    return 1
  fi

  if command -v ping >/dev/null 2>&1; then
    for host in $PING_HOSTS; do
      if ping -I "$iface" -c 1 -W 4 "$host" >/dev/null 2>&1; then
        return 0
      fi
    done
    log_msg "Ping melalui $iface gagal ke host: $PING_HOSTS"
    return 1
  fi

  if [ -r "/sys/class/net/$iface/carrier" ]; then
    read -r carrier < "/sys/class/net/$iface/carrier"
    [ "$carrier" = "1" ] && return 0
    log_msg "Carrier interface $iface down"
    return 1
  fi

  log_msg "Tidak dapat melakukan pengecekan konektivitas (ping tidak tersedia)"
  return 1
}

cleanup() {
  rm -f "$PID_FILE"
  exit 0
}

run_loop() {
  fails=0
  last_state="unknown"
  while :; do
    load_conf
    if check_interface "$IFACE"; then
      if [ "$last_state" != "ok" ]; then
        log_msg "Koneksi interface $IFACE normal"
        last_state="ok"
      fi
      fails=0
    else
      fails=$((fails + 1))
      last_state="fail"
      log_msg "Deteksi kegagalan ($fails/$MAX_ATTEMPTS) pada $IFACE"
      if [ "$fails" -ge "$MAX_ATTEMPTS" ]; then
        log_msg "Batas kegagalan tercapai. Sistem reboot."
        sync
        sleep 2
        if command -v reboot >/dev/null 2>&1; then
          reboot
        elif command -v /sbin/reboot >/dev/null 2>&1; then
          /sbin/reboot
        else
          log_msg "Perintah reboot tidak ditemukan"
        fi
        sleep 60
      fi
    fi
    sleep "$CHECK_INTERVAL"
  done
}

case "${1:-run}" in
  run)
    trap cleanup INT TERM EXIT
    echo "$$" > "$PID_FILE" 2>/dev/null || true
    run_loop
    ;;
  once)
    load_conf
    if check_interface "$IFACE"; then
      echo "OK"
    else
      echo "FAIL"
    fi
    ;;
  *)
    echo "Usage: $0 [run|once]"
    exit 1
    ;;
esac
EOSVC
  chmod 755 "$SERVICE_FILE"
  ok "Service script diperbarui: $SERVICE_FILE"
}

ensure_rc_local_entry() {
  require_root
  entry="/opt/ranet-bot/usb-watchdog-setup.sh start-service >/dev/null 2>&1 &"
  if [ ! -f "$RC_LOCAL" ]; then
    cat > "$RC_LOCAL" <<'EORC'
#!/bin/sh
/opt/ranet-bot/usb-watchdog-setup.sh start-service >/dev/null 2>&1 &
exit 0
EORC
    chmod 755 "$RC_LOCAL"
    ok "rc.local dibuat dan entry startup ditambahkan"
    return
  fi
  if grep -Fq "$entry" "$RC_LOCAL"; then
    ok "rc.local sudah memiliki entry startup"
    return
  fi
  tmp="$(mktemp)"
  sed '/usb-watchdog-setup.sh start-service/d' "$RC_LOCAL" 2>/dev/null | sed '/usb-watchdog.sh run/d' > "$tmp"
  sed -i '/^[[:space:]]*exit[[:space:]]0/d' "$tmp" 2>/dev/null || true
  printf "%s\n" "$entry" >> "$tmp"
  printf "exit 0\n" >> "$tmp"
  cat "$tmp" > "$RC_LOCAL"
  rm -f "$tmp"
  chmod 755 "$RC_LOCAL"
  ok "Entry startup ditambahkan ke rc.local"
}

get_pid() {
  if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null | tr -cd '0-9')"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      printf "%s" "$pid"
      return 0
    fi
    rm -f "$PID_FILE"
  fi
  return 1
}

start_service() {
  require_root
  ensure_service_script
  ensure_rc_local_entry
  if pid=$(get_pid); then
    ok "USB Watchdog sudah berjalan (PID $pid)"
    return 0
  fi
  if command -v start-stop-daemon >/dev/null 2>&1; then
    if start-stop-daemon -S -b -m -p "$PID_FILE" -x "$SERVICE_FILE" -- run; then
      ok "Service USB Watchdog dimulai (start-stop-daemon)"
      return 0
    fi
    err "Gagal menjalankan start-stop-daemon"
    return 1
  fi
  nohup "$SERVICE_FILE" run >/dev/null 2>&1 &
  pid=$!
  echo "$pid" > "$PID_FILE" 2>/dev/null || true
  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    ok "Service USB Watchdog dimulai (nohup)"
    return 0
  fi
  err "Gagal memulai USB Watchdog"
  return 1
}

stop_service() {
  require_root
  if ! pid=$(get_pid); then
    warn "USB Watchdog tidak berjalan"
    return 0
  fi
  if command -v start-stop-daemon >/dev/null 2>&1; then
    start-stop-daemon -K -p "$PID_FILE" >/dev/null 2>&1 || true
  else
    kill "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$PID_FILE" 2>/dev/null || true
  sleep 1
  if pid=$(get_pid); then
    err "Gagal menghentikan USB Watchdog (PID $pid masih aktif)"
    return 1
  fi
  ok "USB Watchdog dihentikan"
  return 0
}

restart_service() {
  require_root
  stop_service >/dev/null 2>&1 || true
  start_service
}

status_service() {
  if pid=$(get_pid); then
    ok "USB Watchdog aktif (PID $pid)"
  else
    warn "USB Watchdog tidak berjalan"
  fi
  if [ -f "$CONF_FILE" ]; then
    say "Config: $CONF_FILE"
  else
    warn "Config belum dibuat. Jalankan setup terlebih dahulu."
  fi
}

interactive_setup() {
  require_root
  load_config
  say "===== Setup USB Watchdog ====="
  say "Kosongkan input untuk mempertahankan nilai saat ini."
  say "Interface saat ini: $CONF_INTERFACE"
  printf "Interface baru [$CONF_INTERFACE]: "
  read -r ans
  [ -n "$ans" ] && CONF_INTERFACE="$(trim "$ans")"

  say "Interval pengecekan (detik) saat ini: $CONF_INTERVAL"
  printf "Interval baru [$CONF_INTERVAL]: "
  read -r ans
  [ -n "$ans" ] && CONF_INTERVAL="$(validate_uint "$ans" 3 "$CONF_INTERVAL")"

  say "Maks. percobaan sebelum reboot: $CONF_MAX_ATTEMPTS"
  printf "Maks percobaan baru [$CONF_MAX_ATTEMPTS]: "
  read -r ans
  [ -n "$ans" ] && CONF_MAX_ATTEMPTS="$(validate_uint "$ans" 1 "$CONF_MAX_ATTEMPTS")"

  say "Lokasi log saat ini: $CONF_LOG_FILE"
  printf "Log file baru [$CONF_LOG_FILE]: "
  read -r ans
  [ -n "$ans" ] && CONF_LOG_FILE="$(trim "$ans")"

  say "Logging diaktifkan (yes/no) saat ini: $CONF_LOGGING"
  printf "Logging? [$CONF_LOGGING]: "
  read -r ans
  [ -n "$ans" ] && CONF_LOGGING="$(normalize_logging "$ans")"

  write_config "$CONF_INTERFACE" "$CONF_INTERVAL" "$CONF_MAX_ATTEMPTS" "$CONF_LOG_FILE" "$CONF_LOGGING"
  ensure_service_script
  ensure_rc_local_entry
  restart_service
}

configure_noninteractive() {
  require_root
  load_config
  interface="$CONF_INTERFACE"
  interval="$CONF_INTERVAL"
  attempts="$CONF_MAX_ATTEMPTS"
  log_file="$CONF_LOG_FILE"
  logging="$CONF_LOGGING"
  auto_start="yes"

  while [ $# -gt 0 ]; do
    case "$1" in
      --interface|-i)
        shift; interface="$(trim "${1:-}")" ;;
      --interval|-t|--check-interval)
        shift; interval="$(validate_uint "${1:-}" 3 "$interval")" ;;
      --max-attempts|-m)
        shift; attempts="$(validate_uint "${1:-}" 1 "$attempts")" ;;
      --log-file|-l)
        shift; log_file="${1:-}" ;;
      --logging|-g|--logging-enabled)
        shift; logging="$(normalize_logging "${1:-}")" ;;
      --no-restart)
        auto_start="no" ;;
      --help)
        say "Opsi configure: --interface IF --interval SEC --max-attempts N --log-file PATH --logging yes|no [--no-restart]"
        return 0 ;;
      *)
        err "Opsi tidak dikenal: $1"
        return 1 ;;
    esac
    shift
  done

  [ -n "$interface" ] || { err "Interface wajib diisi"; return 1; }
  interval="$(validate_uint "$interval" 3 "$DEFAULT_INTERVAL")"
  attempts="$(validate_uint "$attempts" 1 "$DEFAULT_MAX_ATTEMPTS")"
  logging="$(normalize_logging "$logging")"
  [ -n "$log_file" ] || log_file="$DEFAULT_LOG_FILE"

  write_config "$interface" "$interval" "$attempts" "$log_file" "$logging"
  ensure_service_script
  ensure_rc_local_entry
  if [ "$auto_start" = "yes" ]; then
    restart_service
  fi
}

show_status() {
  status_service
  say ""
  print_config
}

interactive_menu() {
  require_root
  while :; do
    say "=================================="
    say " USB Watchdog Setup Menu"
    say "=================================="
    say "1) Setup / Update Config"
    say "2) Lihat Konfigurasi"
    say "3) Start Service"
    say "4) Stop Service"
    say "5) Status Service"
    say "6) Restart Service"
    say "7) List Interface"
    say "8) Keluar"
    printf "Pilih [1-8]: "
    read -r choice
    case "$choice" in
      1) interactive_setup ;;
      2) print_config ;;
      3) start_service ;;
      4) stop_service ;;
      5) show_status ;;
      6) restart_service ;;
      7) list_interfaces ;;
      8) break ;;
      *) warn "Pilihan tidak valid" ;;
    esac
    say ""
  done
}

usage() {
  cat <<'EOUSAGE'
USB Watchdog Setup Script
Usage: usb-watchdog-setup.sh [command]
Commands:
  menu                 Jalankan menu interaktif
  setup                Konfigurasi interaktif
  configure [opsi]     Konfigurasi non-interaktif (lihat --help)
  start-service        Mulai watchdog sebagai daemon
  stop-service         Hentikan watchdog
  restart-service      Restart watchdog
  status|cek-status    Tampilkan status service & config
  show-config          Tampilkan konfigurasi saat ini
  list-if              Tampilkan daftar interface jaringan
  ensure-service       Paksa tulis ulang service script & rc.local
  help|-h|--help       Tampilkan bantuan ini
EOUSAGE
}

cmd="${1:-menu}"
case "$cmd" in
  menu|interactive) shift; interactive_menu "$@" ;;
  setup) shift; interactive_setup "$@" ;;
  configure) shift; configure_noninteractive "$@" ;;
  start-service|start) shift; start_service "$@" ;;
  stop-service|stop) shift; stop_service "$@" ;;
  restart-service|restart) shift; restart_service "$@" ;;
  status|cek-status) shift; show_status "$@" ;;
  show-config) shift; print_config "$@" ;;
  list-if|iflist) shift; list_interfaces "$@" ;;
  ensure-service) shift; ensure_service_script; ensure_rc_local_entry ;;
  help|-h|--help) usage ;;
  *) usage; exit 1 ;;
esac
