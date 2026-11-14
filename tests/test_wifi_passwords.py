"""Saved Wi-Fi passwords: locklyctl's own (keyring, else the config file) and
this computer's (NetworkManager, through a stand-in nmcli). No real keyring,
NetworkManager or lock is used."""
import json
import os
import stat
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from locklyctl import config
from locklyctl import wifi_passwords as W

# A stand-in nmcli: saved connections and their secrets, answering only the
# calls wifi_passwords makes, like nmcli 1.4x with -g / --escape no.
FAKE_NMCLI = r'''#!{python}
import json, sys
nets = json.load(open({nets!r}))
a = sys.argv[1:]
if a[:4] == ["-t", "-f", "UUID,TYPE", "connection"]:
    for n in nets:
        print(n["uuid"] + ":" + n["type"])
elif a[:3] == ["--escape", "no", "-g"] and a[3] == "802-11-wireless.ssid":
    print(next(n["ssid"] for n in nets if n["uuid"] == a[-1]))
elif a[:4] == ["--escape", "no", "-s", "-g"]:
    n = next(n for n in nets if n["uuid"] == a[-1])
    print(n["key_mgmt"]); print(n["psk"] if n["shares"] else "")
else:
    sys.exit(2)
'''


class FakeKeyring:
    """The parts of the keyring module wifi_passwords uses."""
    errors = types.SimpleNamespace(KeyringError=type("KeyringError", (Exception,), {}))
    errors.PasswordDeleteError = type("PasswordDeleteError", (errors.KeyringError,), {})

    def __init__(self, broken=False):
        self.store, self.broken = {}, broken

    def set_password(self, service, user, pw):
        if self.broken:
            raise self.errors.KeyringError("locked")
        self.store[(service, user)] = pw

    def get_password(self, service, user):
        if self.broken:
            raise self.errors.KeyringError("locked")
        return self.store.get((service, user))

    def delete_password(self, service, user):
        if (service, user) not in self.store:
            raise self.errors.PasswordDeleteError("not there")
        del self.store[(service, user)]


def _setup(keyring=None, nets=None):
    """A temp home, the given keyring (None: none works) and stand-in nmcli
    (None: no NetworkManager). Returns a function that undoes it."""
    home = tempfile.mkdtemp()
    old_home, old_kr, old_nmcli = os.environ.get("LOCKLYCTL_HOME"), config._keyring, W.NMCLI
    os.environ["LOCKLYCTL_HOME"] = home
    config._keyring = lambda: keyring
    W.NMCLI = os.path.join(home, "no-nmcli-here")
    if nets is not None:
        with open(os.path.join(home, "nets.json"), "w") as f:
            json.dump(nets, f)
        W.NMCLI = os.path.join(home, "nmcli")
        with open(W.NMCLI, "w") as f:
            f.write(FAKE_NMCLI.format(python=sys.executable, nets=os.path.join(home, "nets.json")))
        os.chmod(W.NMCLI, stat.S_IRWXU)

    def undo():
        if old_home is None:
            os.environ.pop("LOCKLYCTL_HOME", None)
        else:
            os.environ["LOCKLYCTL_HOME"] = old_home
        config._keyring, W.NMCLI = old_kr, old_nmcli
    return undo


NETS = [{"uuid": "u-1", "type": "802-3-ethernet", "ssid": "", "key_mgmt": "", "psk": "", "shares": True},
        {"uuid": "u-2", "type": "802-11-wireless", "ssid": "Cafe: Guest", "key_mgmt": "wpa-psk",
         "psk": "guest pass", "shares": True},
        {"uuid": "u-3", "type": "802-11-wireless", "ssid": "Office", "key_mgmt": "wpa-psk",
         "psk": "kept back", "shares": False},
        {"uuid": "u-4", "type": "802-11-wireless", "ssid": "Open", "key_mgmt": "none", "psk": "", "shares": True}]


def test_saved_in_the_keyring_when_one_works():
    kr = FakeKeyring()
    undo = _setup(keyring=kr)
    try:
        assert W.save("Net A", "pw-1") == "keyring"
        assert kr.store == {("locklyctl-wifi", "Net A"): "pw-1"}
        cfg = config.load_config()
        assert cfg["wifi_in_keyring"] == ["Net A"] and cfg["wifi_passwords"] == {}   # not in the file
        assert W.saved("Net A") == ("pw-1", "") and W.resolve("Net A") == ("pw-1", "saved", "")
        assert W.known(["Net A", "Other"]) == {"Net A": "saved", "Other": ""}
        kr.broken = True                                       # locked: says so rather than returning nothing
        pw, why = W.saved("Net A")
        assert pw is None and "can't be read" in why and "locked" in why
        kr.broken = False
        assert W.forget("Net A") is True and kr.store == {} and W.saved("Net A") == (None, "")
        assert W.forget("Net A") is False
    finally:
        undo()


def test_saved_in_the_config_file_without_a_keyring():
    undo = _setup(keyring=FakeKeyring(broken=True))           # a keyring that refuses: the file instead
    try:
        assert W.save("Net A", "pw-1") == "file"
        path = os.path.join(os.environ["LOCKLYCTL_HOME"], config.CONFIG_FILE_NAME)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600   # owner-only, as the saved login is
        assert config.load_config()["wifi_passwords"] == {"Net A": "pw-1"}
        assert W.resolve("Net A") == ("pw-1", "saved", "")
        assert W.forget("Net A") and config.load_config()["wifi_passwords"] == {}
    finally:
        undo()


def test_this_computers_saved_network():
    undo = _setup(nets=NETS)
    try:
        assert W.from_system("Cafe: Guest") == ("guest pass", "")            # a colon in the name
        pw, why = W.from_system("Office")                                     # NetworkManager kept it back
        assert pw is None and "didn't give its password" in why
        pw, why = W.from_system("Open")                                       # no WPA password to give
        assert pw is None and "not with a WPA" in why
        assert W.from_system("Elsewhere") == (None, "")
        assert W.known(["Cafe: Guest", "Office", "Elsewhere"]) == {
            "Cafe: Guest": "system", "Office": "system", "Elsewhere": ""}
        assert W.resolve("Cafe: Guest") == ("guest pass", "system", "")
        W.save("Cafe: Guest", "mine")                                         # locklyctl's own comes first
        assert W.resolve("Cafe: Guest") == ("mine", "saved", "")
    finally:
        undo()


def test_no_networkmanager():
    undo = _setup()
    try:
        assert W.from_system("Net A") == (None, "") and W.known(["Net A"]) == {"Net A": ""}
        assert W.resolve("Net A") == (None, "", "")
    finally:
        undo()


def test_what_a_setup_result_does_to_the_saved_password():
    undo = _setup()
    try:
        assert W.after_setup("Net A", "typed-pw", "typed", 2) == ""           # didn't connect: not saved
        assert W.saved("Net A") == (None, "")
        assert "Saved the password for Net A" in W.after_setup("Net A", "typed-pw", "typed", 3)
        assert W.saved("Net A") == ("typed-pw", "")
        assert W.after_setup("Net A", "typed-pw", "saved", 3) == ""          # already saved
        note = W.after_setup("Net A", "typed-pw", "saved", 4)                 # the lock calls it wrong
        assert "forgotten" in note and W.saved("Net A") == (None, "")
        assert W.after_setup("Net B", "nm-pw", "system", 3) == ""             # this computer has it already
        assert "this computer's saved password" in W.after_setup("Net B", "nm-pw", "system", 4)
        assert W.saved("Net B") == (None, "")
        note = W.after_setup("Net C", "typed-pw", "file", 3)                  # from --password-file: kept too
        assert "typed-pw" not in note and W.saved("Net C") == ("typed-pw", "")
    finally:
        undo()


# Stand-ins for macOS's `security` and Windows' `netsh`, answering the calls
# wifi_passwords makes as those tools do. Their data comes from the JSON file
# named by LOCKLYCTL_FAKE_DATA.
FAKE_SECURITY = r'''
import json, os, sys
store = json.load(open(os.environ["LOCKLYCTL_FAKE_DATA"]))
a = sys.argv[1:]
if a[:4] != ["find-generic-password", "-D", "AirPort network password", "-a"]:
    sys.exit(2)
item = store.get(a[4])
if item is None:
    print("security: The specified item could not be found in the keychain.", file=sys.stderr)
    sys.exit(44)                                   # errSecItemNotFound
if "-w" in a[5:]:
    if item.get("deny"):
        sys.exit(128)                              # the administrator prompt was cancelled
    print(item["pw"])
else:
    print('keychain: "/Library/Keychains/System.keychain"')
'''

FAKE_NETSH = r'''
import json, os, sys
from xml.sax.saxutils import escape
data = json.load(open(os.environ["LOCKLYCTL_FAKE_DATA"]))
a = sys.argv[1:]
if a[:3] != ["wlan", "export", "profile"]:
    sys.exit(1)
opts = dict(arg.partition("=")[::2] for arg in a[3:])
folder, name = opts["folder"].strip('"'), opts.get("name", "").strip('"')
with open(os.environ["LOCKLYCTL_FAKE_DATA"] + ".folders", "a") as log:
    log.write(folder + "\n")
for p in data:
    if name and p["name"] != name:
        continue
    plain = opts.get("key") == "clear" and p["clear_ok"]
    key = p["key"] if plain else "01000000D08C9DDF0115D1118C7A00C04FC297EB0100"
    xml = ('<?xml version="1.0"?>\n<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
           f'<name>{escape(p["name"])}</name><SSIDConfig><SSID><hex>00</hex><name>{escape(p["ssid"])}</name></SSID>'
           '</SSIDConfig><connectionType>ESS</connectionType><MSM><security><authEncryption>'
           f'<authentication>{p["auth"]}</authentication><encryption>AES</encryption><useOneX>false</useOneX>'
           f'</authEncryption><sharedKey><keyType>passPhrase</keyType><protected>{"false" if plain else "true"}'
           f'</protected><keyMaterial>{escape(key)}</keyMaterial></sharedKey></security></MSM></WLANProfile>')
    with open(os.path.join(folder, "Wi-Fi-" + p["name"] + ".xml"), "w", encoding="utf-8") as f:
        f.write(xml)
print('Interface profile is saved in file')
'''


def _fake_tool(platform, attr, script, data):
    """Point wifi_passwords at a stand-in tool for ``platform``; returns undo."""
    home = tempfile.mkdtemp()
    path = os.path.join(home, attr.lower())
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n" + script)
    os.chmod(path, stat.S_IRWXU)
    data_path = os.path.join(home, "data.json")
    with open(data_path, "w") as f:
        json.dump(data, f)
    old = (W.PLATFORM, getattr(W, attr), os.environ.get("LOCKLYCTL_FAKE_DATA"))
    undo_home = _setup()
    W.PLATFORM, os.environ["LOCKLYCTL_FAKE_DATA"] = platform, data_path
    setattr(W, attr, path)

    def undo():
        W.PLATFORM = old[0]
        setattr(W, attr, old[1])
        if old[2] is None:
            os.environ.pop("LOCKLYCTL_FAKE_DATA", None)
        else:
            os.environ["LOCKLYCTL_FAKE_DATA"] = old[2]
        undo_home()
    return undo, data_path


def test_macos_keychain():
    undo, _ = _fake_tool("darwin", "SECURITY", FAKE_SECURITY,
                         {"Home Net": {"pw": "mac pw"}, "Refused": {"pw": "x", "deny": True}})
    try:
        assert W.known(["Home Net", "Refused", "Elsewhere"]) == {
            "Home Net": "system", "Refused": "system", "Elsewhere": ""}
        assert W.from_system("Home Net") == ("mac pw", "")
        pw, why = W.from_system("Refused")
        assert pw is None and "exited 128" in why and "administrator" in why
        assert W.from_system("Elsewhere") == (None, "")
        assert W.resolve("Home Net") == ("mac pw", "system", "")
        assert W.source_text("system") == "saved on this computer (the macOS keychain)"
    finally:
        undo()


def test_windows_profiles():
    undo, data_path = _fake_tool("win32", "NETSH", FAKE_NETSH, [
        {"name": "Office", "ssid": "Office & Co", "auth": "WPA2PSK", "key": "win pw", "clear_ok": True},
        {"name": "Admin only", "ssid": "Locked Net", "auth": "WPA3SAE", "key": "zz", "clear_ok": False},
        {"name": "Cafe", "ssid": "Cafe", "auth": "open", "key": "", "clear_ok": True}])
    try:
        assert W.known(["Office & Co", "Locked Net", "Cafe", "Elsewhere"]) == {
            "Office & Co": "system", "Locked Net": "system", "Cafe": "system", "Elsewhere": ""}
        assert W.from_system("Office & Co") == ("win pw", "")                 # by profile name, then SSID
        pw, why = W.from_system("Locked Net")
        assert pw is None and "administrator" in why
        pw, why = W.from_system("Cafe")
        assert pw is None and "not with a WPA password" in why
        assert W.from_system("Elsewhere") == (None, "")
        with open(data_path + ".folders") as f:
            folders = f.read().split()
        assert folders and not any(os.path.exists(d) for d in folders)    # files with keys in clear are gone
        assert W.source_text("system") == "saved on this computer (Windows)"
    finally:
        undo()
