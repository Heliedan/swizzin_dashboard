"""The blocks of the Overview layout of the dashboard (templates/overview.html), in the order they show.

Each block has its template in templates/overview/, which keeps the conditions under which it shows (the admin only,
no disk quota...): an account that may not see a block gets nothing from it. Each block shows in one zone of the page:
  glance       the heading of At a glance, after its title (the admin's pills: reboot, updates);
  tiles        At a glance, the row of tiles at the top;
  row          the row under the tiles: the bandwidth graph and Service Info;
  left, right  the two columns of Details, where each block is a section: a <details> element whose data-section is
               the block's id (the browser keeps the open ones by it). One column on a narrow screen, left first.
A new block is one more line here, with its script or its stylesheet if it has one (in static/): templates/index.html
loads those of the Overview layout from here, after the layout's own (SCRIPTS, STYLESHEETS). The id stays the same
for good; the label is the block's name for people. tests/test_overview_blocks.py checks this list.
Each account may move, hide or (in Details) put in the other column the blocks it sees, with Customize
(core/layouts.py, static/js/arrange.js): a new block can be arranged like the others, with nothing more to write."""

ZONES = ("glance", "tiles", "row", "left", "right")

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
    {"id": "cpu", "zone": "left", "template": "overview/cpu.html", "label": "CPU Info"},
    {"id": "ram", "zone": "left", "template": "overview/ram.html", "label": "RAM Info"},
    {"id": "disks", "zone": "right", "template": "overview/disks.html", "label": "Disk Info"},
    {"id": "quota", "zone": "right", "template": "overview/quota.html", "label": "Disk Quota Info"},
    {"id": "network", "zone": "right", "template": "overview/network.html", "label": "Network Info"},
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
