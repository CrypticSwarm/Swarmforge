# Windows (WSL)

Swarmforge runs inside a WSL 2 Ubuntu distro, with Docker Desktop on Windows as the daemon.
Native Windows shells are not supported: the Makefile recipes need bash, and the launcher relies on POSIX-only calls.

This page assumes [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) is already installed.
Steps 1 and 2 happen on the Windows side; everything after runs in the Ubuntu shell.

## 1. Install Ubuntu in WSL

In PowerShell, as Administrator:

```powershell
wsl --version
wsl --list --verbose
```

| What you see | What it means | What to do |
|---|---|---|
| `wsl` is not recognized | WSL isn't installed | `wsl --install -d Ubuntu-26.04`, then reboot |
| `wsl --version` prints help text or "invalid option" | An old built-in WSL | `wsl --update`, then `wsl --install -d Ubuntu-26.04` |
| Only `docker-desktop` (and maybe `docker-desktop-data`) listed | Docker Desktop's own distro, which is not for interactive use | `wsl --install -d Ubuntu-26.04` |
| An `Ubuntu…` distro already listed | An existing install | Run `wsl -d <that name> -- lsb_release -rs`. On 22.04 or newer, keep it. On anything older, install `Ubuntu-26.04` next to it: 20.04's Python is too old. |

Make Ubuntu the default, so `wsl` opens it rather than Docker's distro:

```powershell
wsl --set-default Ubuntu-26.04
wsl --list --verbose     # Ubuntu-26.04 has the * and VERSION 2
```

Use the name the list shows if you kept an existing distro.
Ubuntu asks for a Linux username and password the first time it opens; they need not match your Windows account.
To reset a forgotten password on an existing distro, run `wsl -d Ubuntu -u root passwd <username>`; `wsl -d Ubuntu whoami` prints the username.

From here on, open Ubuntu with `wsl ~` from a regular PowerShell, or from the Start menu.
The `~` starts the shell in your Linux home; plain `wsl` starts in PowerShell's current folder, translated to a path under `/mnt/c`.

If the install fails:

- **`0x80370102`, or "virtualization not enabled":** turn on virtualization (Intel VT-x or AMD-V/SVM) in the BIOS/UEFI, and **Virtual Machine Platform** under *Turn Windows features on or off*. A Docker Desktop already running on its WSL 2 engine means both are on.
- **Microsoft Store blocked:** `wsl --install --web-download -d Ubuntu-26.04`.
- **`wsl --install` not supported:** Windows 10 needs version 2004 (build 19041) or later; `winver` shows yours.

## 2. Connect Docker Desktop to Ubuntu

Start Docker Desktop; Ubuntu reaches Docker only while it runs. Then:

- **Settings → General**: check **Use the WSL 2 based engine**.
- **Settings → Resources → WSL integration**: turn on your Ubuntu distro and click **Apply & restart**. With **Enable integration with my default WSL distro** checked and Ubuntu as the default, it is already on.

## 3. Check Docker from Ubuntu

```bash
docker version
docker run --rm hello-world
```

The **Server** section of `docker version` names **Docker Desktop**.
If it names a plain Docker Engine, this distro has its own Docker from an earlier install; remove it (`sudo apt remove docker-ce docker.io`) so `docker` reaches Docker Desktop.

If `docker` isn't found, or `docker version` has no Server section, run `wsl --shutdown` in PowerShell, restart Docker Desktop, and open Ubuntu again.
If `/var/run/docker.sock` then refuses with `permission denied`, run `sudo usermod -aG docker $USER` followed by another `wsl --shutdown`.

## 4. Install the prerequisites

```bash
sudo apt update && sudo apt install -y git make python3
```

Set your git identity here: sessions mount the Linux `~/.gitconfig`, not the Windows one.

```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

## 5. Install Swarmforge

Clone under your Linux home, and keep the projects you run sessions on there too:

```bash
mkdir -p ~/src && cd ~/src
git clone https://github.com/CrypticSwarm/Swarmforge.git
cd Swarmforge
```

Then follow the [installation steps](../README.md#installation).
After `bash ./install.sh`, run `exec bash -l`: Ubuntu's `~/.profile` puts `~/.local/bin` on `PATH` only if it existed at login.
