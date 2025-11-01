#!/bin/bash
# ================================================================
# Netbird Watchdog Setup Script (CLI + Menu)
# By Ridhan — mendukung interaktif dan sekali-jalan via argumen
# ================================================================

# Pastikan dijalankan sebagai root
if [ "$EUID" -ne 0 ]; then
    echo "Harap jalankan script ini sebagai root (gunakan sudo)"
    exit 1
fi

CONFIG_FILE="/etc/netbird-watchdog.conf"

# ---------- Fungsi ----------
create_watchdog() {
    echo "Membuat file netbird-watchdog.sh..."
    cat > /usr/bin/netbird-watchdog.sh << 'EOF'
#!/bin/bash
CONFIG_FILE="/etc/netbird-watchdog.conf"
TARGET_IP="100.99.153.97"
if [ -f "$CONFIG_FILE" ]; then source "$CONFIG_FILE"; fi
STATUS=$(/etc/init.d/netbird status)
if echo "$STATUS" | grep -q "Stopped"; then
    echo "Netbird stopped — restarting..."
    netbird service install 2>/dev/null
    /etc/init.d/netbird start
else
    echo "Netbird running."
fi
netbird up

check_connection() {
    local timeout=120 fail_count=0 interval=10
    for ((i=0; i<timeout; i+=interval)); do
        if ! ping -c 4 "$TARGET_IP" >/dev/null 2>&1; then
            ((fail_count++))
            echo "Ping failed $fail_count× at $(date)"
        else
            echo "Ping success at $(date)"
            return 0
        fi
        sleep $interval
    done
    if [ $fail_count -ge $((timeout/interval)) ]; then
        echo "2 menit gagal, restart Netbird..."
        netbird down; sleep 5; netbird up
        echo "Netbird restarted $(date)"
    fi
}
while true; do check_connection; sleep 60; done
EOF
    chmod +x /usr/bin/netbird-watchdog.sh
    echo "[OK] netbird-watchdog.sh dibuat."
}

backup_rc_local() {
    [ -f /etc/rc.local ] && cp /etc/rc.local /etc/rc.local.bak && echo "[OK] Backup rc.local" || echo "[WARN] rc.local belum ada"
}

restore_rc_local() {
    [ -f /etc/rc.local.bak ] && cp /etc/rc.local.bak /etc/rc.local && chmod +x /etc/rc.local && echo "[OK] Restore rc.local" || echo "[ERR] Backup tidak ditemukan"
}

edit_rc_local() {
    if [ ! -f /etc/rc.local ]; then
        cat > /etc/rc.local << 'EOF'
#!/bin/sh -e
/usr/bin/netbird-watchdog.sh &
exit 0
EOF
    else
        grep -q "/usr/bin/netbird-watchdog.sh &" /etc/rc.local || sed -i '/exit 0/i /usr/bin/netbird-watchdog.sh &' /etc/rc.local
    fi
    chmod +x /etc/rc.local
    echo "[OK] rc.local diatur."
}

remove_config() {
    rm -f /usr/bin/netbird-watchdog.sh "$CONFIG_FILE"
    sed -i '/\/usr\/bin\/netbird-watchdog.sh &/d' /etc/rc.local 2>/dev/null || true
    echo "[OK] Semua konfigurasi dihapus."
}

change_ip() {
    local new_ip="$1"
    local default_ip="100.99.153.97"
    if [ -z "$new_ip" ]; then new_ip="$default_ip"; fi
    echo "TARGET_IP=\"$new_ip\"" > "$CONFIG_FILE"
    echo "[OK] IP diatur ke $new_ip"
}

check_status() {
    local ip
    ip=$(grep "TARGET_IP=" "$CONFIG_FILE" 2>/dev/null | cut -d'"' -f2)
    [ -z "$ip" ] && ip="100.99.153.97"
    if ping -c 4 "$ip" >/dev/null 2>&1; then
        echo "[OK] Ping sukses ke $ip"
    else
        echo "[FAIL] Tidak bisa ping $ip"
    fi
}

check_service_status() {
    if pgrep -f "/usr/bin/netbird-watchdog.sh" >/dev/null; then
        echo "[OK] Service berjalan (PID $(pgrep -f "/usr/bin/netbird-watchdog.sh"))"
    else
        echo "[WARN] Service tidak berjalan"
    fi
}

# ---------- Mode Sekali Jalan ----------
if [ $# -gt 0 ]; then
    case "$1" in
        setup)
            backup_rc_local
            create_watchdog
            edit_rc_local
            echo "[DONE] Setup selesai."
            ;;
        backup) backup_rc_local ;;
        restore) restore_rc_local ;;
        remove) remove_config ;;
        ganti-ip)
            change_ip "$2"
            ;;
        cek-status) check_status ;;
        cek-service) check_service_status ;;
        *)
            echo "Argumen tidak dikenal. Gunakan: setup | backup | restore | remove | ganti-ip <ip> | cek-status | cek-service"
            ;;
    esac
    exit 0
fi

# ---------- Mode Interaktif ----------
echo "Script Setup Netbird Watchdog"
echo "1. Setup"
echo "2. Backup rc.local"
echo "3. Restore rc.local"
echo "4. Hapus konfigurasi"
echo "5. Ganti IP target"
echo "6. Cek status ping"
echo "7. Cek status service"
echo "8. Keluar"
read -p "Masukkan pilihan (1-8): " choice
case $choice in
    1) backup_rc_local; create_watchdog; edit_rc_local; echo "Setup selesai." ;;
    2) backup_rc_local ;;
    3) restore_rc_local ;;
    4) remove_config ;;
    5) read -p "Masukkan IP baru: " ip; change_ip "$ip" ;;
    6) check_status ;;
    7) check_service_status ;;
    8) exit 0 ;;
    *) echo "Pilihan tidak valid!" ;;
esac
