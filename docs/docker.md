# Running jamp in Docker

For a library on a NAS (Unraid, Synology, TrueNAS) or any machine where Docker
is already how things run. On a desktop, the standalone download or `pip
install` is simpler - see [guide.md](guide.md#3-install-and-set-up).

The image includes ffmpeg with the Shorten decoder, so `jamp convert` works
out of the box.

## Build the image

From a copy of this repository:

```bash
docker build -t jamp .
```

## Three folders

| inside the container | what to mount |
| --- | --- |
| `/music` | your live music library |
| `/reports` | a folder **outside** the library, for reports |
| `/config` | a volume or folder for your settings, overrides, remembered paths, and phase 3's download cache (`/config/cache`) |

The tool refuses to put reports inside the library, so `/reports` must not be
a folder within the one mounted on `/music`.

## Set up, once

```bash
docker run --rm -it \
  -v jamp-config:/config \
  -v /mnt/user/music/live:/music \
  -v /mnt/user/jamp-reports:/reports \
  jamp init --root /music --out-dir /reports
```

The paths it remembers are the container's (`/music`, `/reports`), so every
later run mounts the same three folders and leaves the paths out.

## Run anything

The same command with a different ending. A shell alias saves typing:

```bash
alias jamp='docker run --rm -it -v jamp-config:/config \
  -v /mnt/user/music/live:/music -v /mnt/user/jamp-reports:/reports jamp'

jamp doctor
jamp unpack
jamp convert
jamp acts
jamp phase1 --artist "Grateful Dead"
jamp phase2 --artist "Grateful Dead" --commit --until-settled
```

Everything in [the guide](guide.md) applies unchanged.

## File ownership

A container runs as root unless told otherwise, and files it renames or
creates would then belong to root. Run it as the user that owns the library:

```bash
docker run --rm -it --user 99:100 ...      # Unraid's nobody:users
docker run --rm -it --user 1026:100 ...    # a typical Synology user
```

(`id <username>` on the NAS gives the numbers.) The `/config` volume must be
writable by that user too.

## Docker Compose

```yaml
services:
  jamp:
    build: .
    image: jamp
    user: "99:100"
    volumes:
      - ./config:/config
      - /mnt/user/music/live:/music
      - /mnt/user/jamp-reports:/reports
```

```bash
docker compose run --rm jamp doctor
docker compose run --rm jamp phase1 --artist "Phish"
```

## Notes

- **Case-sensitive file systems.** A NAS library is usually case-sensitive,
  where Windows is not; two folders differing only in case are two folders.
- **`tools/`** scripts are not in the image. They are maintenance scripts for
  a copy of the repository.
- **Long runs** (`phase0 --verify-audio`, `convert` on a large library) keep
  going only while the command does; on a NAS, run them inside `screen`,
  `tmux` or the NAS's own task scheduler rather than an SSH session that may
  close.
