# MYT file upload notes

## Protocol

MYT documents local upload as:

- `POST http://<controller-host>:<device-port>/upload`
- `multipart/form-data`
- form field `file`

Official API:
<https://dev.moyunteng.com/docs/NewMYTOS/MYT_ANDROID_API>

## Actual landing directory

Real-device testing from this Skill's first version showed that a successful
`/upload` response stored the multipart filename under `/sdcard/upload`, not
`/sdcard/Download`. The script therefore:

1. Uploads an ASCII temporary filename into `/sdcard/upload`.
2. Writes its remote byte count to a tiny marker via MYT shell.
3. Downloads only that marker for verification.
4. Moves the verified temporary file to its final relative path.
5. Triggers Android media scanning.

The default final directory is also `/sdcard/upload`. `--remote-dir` may point to
another safe Android absolute path; staging still occurs in `/sdcard/upload`.

## Directory behavior

`--path` accepts either:

- One regular file, uploaded to the remote directory.
- One directory, recursively enumerated in stable name order.

Relative subdirectories are preserved. Symbolic links and empty directories are
not uploaded because the MYT endpoint transfers files rather than directory
objects. All regular file extensions are accepted.

## Device mapping

| Device | Port |
|---|---:|
| `T1001` | `10005` |
| `T1002` | `10008` |
| `T1003` | `10011` |

Formula: `10005 + (device_index - 1) * 3`.

## Troubleshooting

- `Local file or directory path is required`: the agent failed to pass the path
  supplied by the user. Use `--path "<exact path>"`.
- `Directory contains no regular files`: the selected folder is empty or only
  contains symbolic links/directories.
- `temporary upload size mismatch`: confirm the script stages under
  `/sdcard/upload`; do not change the staging path to `/sdcard/Download`.
- `conflict`: rerunning is safe, but replacement requires explicit `--overwrite`.
- Partial directory result: rerun the same directory only for failed devices;
  verified files will return `already-present`.
