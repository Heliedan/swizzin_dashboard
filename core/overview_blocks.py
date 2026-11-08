"""The blocks of the Overview layout of the dashboard (templates/overview.html), in the order they show.

Each block has its template in templates/overview/, which keeps the conditions under which it shows (the admin only,
no disk quota...): an account that may not see a block gets nothing from it. Each block shows in one zone of the page:
  glance       the heading of At a glance, after its title (the admin's pills: reboot, updates);
  tiles        At a glance, the row of tiles at the top;
  row          the row under the tiles: the bandwidth graph and Service Info;
  left, right  the two columns of Details, where each block is a section: a <details> element whose data-section is
               the block's id (the browser keeps the open ones by it). One column on a narrow screen, left first;
  wide         under the two columns, the sections of Details that take its whole width (their content is laid out
               across it when it is wide enough, in their own stylesheet).
Two flags give a block's state by default, before an account changes it:
  hidden       True: the block is hidden by default (it repeats what another one shows, or is seldom needed);
               Customize shows it again;
  open         True: the section of Details is open by default (its template renders the open attribute from it);
               the browser still remembers it closed when it is closed (static/js/overview.js).
A new block is one more line here, with its script or its stylesheet if it has one (in static/): templates/index.html
loads those of the Overview layout from here, after the layout's own (SCRIPTS, STYLESHEETS). The id stays the same
for good; the label is the block's name for people. tests/test_overview_blocks.py checks this list.
Each account may move, hide or show, or (in a column of Details) put in the other column the blocks it sees, with
Customize (core/layouts.py, static/js/arrange.js): a new block can be arranged like the others, with nothing more to
write."""

ZONES = ("glance", "tiles", "row", "left", "right", "wide")

STYLESHEETS = ["css/overview.css"]
SCRIPTS = ["js/overview.js", "js/arrange.js"]  # arrange.js: Customize, each account's order (core/layouts.py)

BLOCKS = [
    {"id": "system-status", "zone": "glance", "template": "overview/system_status.html", "label": "Reboot and updates",
     "script": "js/system_status.js"},
    {"id": "tile-cpu", "zone": "tiles", "template": "overview/tile_cpu.html", "label": "CPU"},
    {"id": "tile-ram", "zone": "tiles", "template": "overview/tile_ram.html", "label": "Memory"},
    {"id": "tile-disk", "zone": "tiles", "template": "overview/tile_disk.html", "label": "Disk /"},
    {"id": "tile-net", "zone": "tiles", "template": "overview/tile_net.html", "label": "Network"},
    {"id": "bandwidth", "zone": "row", "template": "overview/bandwidth.html", "label": "Network"},
    {"id": "services", "zone": "row", "template": "overview/services.html", "label": "Service Info",
     "script": "js/service_logs.js"},
    # Hidden by default, for a shorter page: CPU Info and RAM Info mostly repeat the CPU and Memory tiles; Disk Info
    # and Panel sign-ins are seldom needed at a glance (the disk almost full banner still warns of a full disk).
    # Customize shows them again, for whoever wants their detail (each core, user, system and I/O wait, the uptime,
    # every disk and its speeds, the sign-ins).
    {"id": "cpu", "zone": "left", "template": "overview/cpu.html", "label": "CPU Info", "hidden": True},
    {"id": "ram", "zone": "left", "template": "overview/ram.html", "label": "RAM Info", "hidden": True},
    {"id": "disks", "zone": "right", "template": "overview/disks.html", "label": "Disk Info", "hidden": True},
    {"id": "quota", "zone": "right", "template": "overview/quota.html", "label": "Disk Quota Info"},
    {"id": "signins", "zone": "right", "template": "overview/signins.html", "label": "Panel sign-ins",
     "script": "js/signins.js", "hidden": True},
    {"id": "network", "zone": "wide", "template": "overview/network.html", "label": "Network Info",
     "stylesheet": "css/network.css", "open": True},
    {"id": "processes", "zone": "wide", "template": "overview/processes.html", "label": "Top processes",
     "script": "js/processes.js", "stylesheet": "css/processes.css", "open": True},
]


def zone(name):
    """The blocks of the zone `name`, in their order."""
    return [block for block in BLOCKS if block["zone"] == name]


def scripts():
    """The scripts of the Overview layout, in static/: its own, then those of its blocks."""
    return SCRIPTS + [block["script"] for block in BLOCKS if block.get("script")]


def stylesheets():
    """The stylesheets of the Overview layout, in static/: its own, then those of its blocks."""
    return STYLESHEETS + [block["stylesheet"] for block in BLOCKS if block.get("stylesheet")]
