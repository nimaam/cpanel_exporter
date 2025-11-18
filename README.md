## **What this exporter does**
The **cPanel Prometheus Exporter** is a small Python/Flask service that:
-   Runs **on a cPanel/WHM server as root**
-   Uses **WHM API 1 (****whmapi1****)** to list **all cPanel accounts** 
-   For **each cPanel user**, calls **UAPI (****uapi --user=<cpuser>****)** to collect:
    -   **StatsBar** metrics (disk usage, bandwidth, domains, etc.)   
    -   **CloudLinux LVE usage** (CPU, memory, IO, etc.) via ResourceUsage::get_usages
    -   **MySQL** database disk usage  
    -   **PostgreSQL** database disk usage (if enabled)   
    -   **Email** POP accounts disk usage    
    -   **FTP** accounts disk usage
-   Exposes everything on a single HTTP endpoint:  
    http://<server-ip>:9123/metrics

**Installation**

***Get the code***
```
mkdir -p /opt/cpanel-exporter
cd /opt/cpanel-exporter
git clonehttps://github.com/nimaam/cpanel_exporter .
```

***Installation the requirements:***
```
python3 -m pip install --upgrade pip
python3 -m pip install flask
```

***Add the services:***
```
nano /etc/systemd/system/cpanel-exporter.service
```

***cpanel-exporter.service file:***
```
[Unit]
Description=Prometheus cPanel Exporter (All Users)
After=network.target

[Service]
Type=simple
User=root
Group=root

ExecStart=/usr/bin/python3 /opt/cpanel-exporter/cpanel_exporter.py --host 0.0.0.0 --port 9123

WorkingDirectory=/opt/cpanel-exporter

Restart=always
RestartSec=3

Environment=LC_ALL=C.UTF-8
Environment=LANG=C.UTF-8

[Install]
WantedBy=multi-user.target
```

***configure and enable the service:***
```
systemctl daemon-reload
systemctl enable --now cpanel_exporter
systemctl status cpanel_exporter
```

***local test:***
```
curl http://YOUR_PUBLIC_IP:9123/metrics | head
```

***test on public IP:***
```
curl http://YOUR_PUBLIC_IP:9123/metrics | head
```

***Prometheus setting:***
```
scrape_configs:
  - job_name: 'cpanel_server'
    static_configs:
      - targets:
          - 'YOUR_SERVER_PUBLIC_IP:9123'
```

