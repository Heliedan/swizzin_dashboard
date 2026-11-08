import pwd

class Config:
    try:
        ADMIN_USER = pwd.getpwuid(1000).pw_name
    except:
        ADMIN_USER = None
    APPLICATION_ROOT = '/'
    FLASK_HTPASSWD_PATH = '/etc/htpasswd'
    FLASK_AUTH_REALM = "What's the password?"
    HOST = "0.0.0.0"
    PORT = "8333"
    RATELIMIT_ENABLED = True
    RATELIMIT_DEFAULT = "5 per minute"
    URLBASE = ""
    SHAREDSERVER = False
    FORMS_LOGIN = True
    NETWORK_TOTALS = False
    DISK_UNITS = "iec"
    DASHBOARD_LAYOUT = "classic"  # or "overview": the layout of the accounts that chose none (core/layouts.py)
    UPDATES_CHECK = True  # Overview: count the pending updates of apt for the admin (core/system_status.py)
    SERVICE_LOGS = True  # the admin's Logs buttons of Service Info, in Overview (core/service_logs.py)
    SERVICE_LOGS_LINES = 100  # lines of the journal they show, 1 to 200

    NETWORK_INFO_CUTOFF = "YYYY-MM-DD"
    NETWORK_INFO_TOP = True
    NETWORK_INFO_TOPSTATS = 5
    NETWORK_INFO_TOTALS = True