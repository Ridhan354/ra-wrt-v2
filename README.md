## Instalasi Otomatis (disarankan)

1. Masuk ke perangkat melalui SSH.
2. Unduh dan jalankan skrip installer (seluruh dependensi, bot Telegram, dan utilitas USB watchdog akan diunduh otomatis):

   ```sh
   curl -fsSLO https://raw.githubusercontent.com/Ridhan354/ra-wrt-v2/blob/main/ranet-manager.sh
   chmod +x ranet-manager.sh
   ./setup-rabot.sh
   ```

3. Ikuti prompt untuk memasukkan BOT TOKEN dan Chat ID admin. Jika variabel lingkungan `BOT_TOKEN` dan `CHAT_ID` sudah diisi, skrip melewati pertanyaan tersebut.
4. Setelah instalasi selesai, bot dijalankan sebagai service (`procd` di OpenWrt atau `systemd` di Ubuntu/Debian) dan akan otomatis aktif setelah reboot. Skrip juga menyalin `usb-watchdog-setup.sh` ke `/opt/ranet-bot/` sehingga menu Telegram dapat langsung mengelola watchdog tanpa langkah manual tambahan.
