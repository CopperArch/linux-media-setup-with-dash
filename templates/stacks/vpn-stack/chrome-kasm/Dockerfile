FROM kasmweb/chrome:1.16.0

# Rename the KasmVNC login username from the default 'kasm_user' to '{{DASH_USER}}'.
# The username is hardcoded in the vendor's startup script (kasmvncpasswd -u kasm_user),
# not exposed as an env var, so we patch the literal token here.
USER root
RUN sed -i -E 's/\bkasm_user\b/{{DASH_USER}}/g' /dockerstartup/vnc_startup.sh && \
    sed -i -E 's/-websocketPort \$NO_VNC_PORT/-websocketPort $NO_VNC_PORT -DisableBasicAuth=1/g' /dockerstartup/vnc_startup.sh

# Magnet links -> qBittorrent. Three pieces, all of which must live in the image
# (not /home/kasm-user) so they survive a recreate that resets the config volume:
#   1. the handler script,
#   2. the .desktop entry that claims x-scheme-handler/magnet for xdg-open,
#   3. a Chrome policy so Chrome launches the handler instead of prompting
#      "Open qBittorrent Magnet Handler?" on every single click.
COPY magnet-to-qbt.sh /usr/local/bin/magnet-to-qbt.sh
COPY magnet-handler.desktop /usr/share/applications/magnet-handler.desktop
COPY magnet-autolaunch.json /etc/opt/chrome/policies/managed/magnet-autolaunch.json
RUN chmod 755 /usr/local/bin/magnet-to-qbt.sh && \
    chmod 644 /usr/share/applications/magnet-handler.desktop \
              /etc/opt/chrome/policies/managed/magnet-autolaunch.json && \
    update-desktop-database /usr/share/applications || true

USER 1000
