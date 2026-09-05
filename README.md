# Bulk Downloader for ASF Data

A small, single-file bulk downloader for Alaska Satellite Facility (ASF) /
NASA Earthdata products.

It handles Earthdata Login, resumes interrupted transfers, and verifies that a
file is complete before accepting it. Its guiding rule is to **fail clearly
rather than leave a corrupt file behind**.



---

## Requirements

* Python **3.10** or newer
* [`requests`](https://pypi.org/project/requests/) — the only third-party dependency

```bash
pip install requests
```

---

## Usage

The simplest way to use this tool is to hand it the download script that ASF
Vertex generates for your search results:

```bash
python ./bulk_downloader.py ./download-all-YYYY-MM-DD_HH-MM-SS.py -o ./data
```

> **Workflow:** search in [ASF Vertex](https://search.asf.alaska.edu) → add the
> granules you want to your downloads → choose the Python download option →
> hand the generated `download-all-*.py` file to this tool.

There is no need to open or edit the generated script. This tool **reads the
product URLs out of the file without executing it** — the file is parsed as data
with Python's `ast` module and is never imported, `exec`'d, `eval`'d or run as a
subprocess.

Only ASF's product list is read. The generated script also contains login
endpoints and documentation links, which are not download targets and are
ignored. Products keep their original order and duplicates are removed. If the
file does not have the structure this tool recognises, it says so and extracts
nothing rather than guessing — should ASF change its generator, use one of the
other input formats below.

### Other input formats

```bash
# One or more URLs
python ./bulk_downloader.py https://.../S1A_IW_SLC__1SDV_....zip

# A .metalink or .csv exported from ASF Vertex
python ./bulk_downloader.py downloads.metalink -o ./data

# A plain text file with one URL per line ('#' starts a comment)
python ./bulk_downloader.py -i urls.txt -o ./data
```

Input types are detected automatically and may be mixed in one command. You can
also populate the `URLS = [...]` list near the bottom of the script and run it
with no arguments.

The exit status is `0` when nothing failed, `1` when at least one file failed,
and `2` when there was nothing to do.

### Options

| Option | Meaning |
| --- | --- |
| `-i`, `--input-file FILE` | Extra input file; may be repeated |
| `-o`, `--output-dir DIR` | Where products are written (default: current directory) |
| `--retries N` | Retry attempts per file for transient errors (default: 3) |
| `--no-resume` | Always restart partial downloads from zero |
| `--checksums FILE` | `md5sum`-style file used to verify each product |
| `--cookie-jar PATH` | Cookie jar location (default: `~/.bulk_download_cookiejar.txt`) |
| `--insecure` | Skip TLS verification — only for a trusted source behind a broken proxy |
| `--quiet` | Suppress the progress display |
| `--no-login` | Skip Earthdata login (only works for openly accessible files) |
| `--logout` | Delete the stored cookie jar and exit |

---

## Earthdata Login

The first run prompts for your NASA Earthdata username and password. They are
used once, to obtain a session from Earthdata Login, and are never stored,
echoed or written to disk. The resulting session is kept in
`~/.bulk_download_cookiejar.txt` (created readable only by you), so later runs
do not prompt again until it expires.

For products served from ASF's Earthdata Cloud endpoints — including NISAR — the
access token from that session is sent as a bearer token. This happens
automatically.

Use `--logout` to discard the stored session, and `--no-login` for the few ASF
resources that are served without authentication.

> **First-time users:** log in to [ASF Vertex](https://search.asf.alaska.edu)
> once, accept the EULA for the dataset you want, and set your Study Area at
> <https://urs.earthdata.nasa.gov>. ASF will not serve data otherwise, and no
> download tool can work around that.

If a download fails with `401` or `403`, check your Earthdata Login session,
dataset EULA acceptance and Study Area settings first.

---

## Downloads, resume and integrity

Nothing is written under the final filename until the file is known to be
complete. Downloads land in `<name>.part` alongside a small `<name>.part.json`
note recording what is being fetched; only when the transfer matches the size
the server announced is the file renamed into place.

* **Existing files** are skipped when their size matches the server exactly. A
  file of any other size is treated as incomplete, set aside as
  `<name>.incomplete`, and downloaded again.
* **Interrupted transfers** (Ctrl-C, a dropped connection, a crash) leave the
  `.part` file in place. Running the same command again resumes from where it
  stopped, using an HTTP range request.
* **Resume is verified, not assumed.** If the server does not honour the range
  request, or the file changed since the partial download, the transfer restarts
  from zero rather than appending to bytes that no longer match.
* **Transient failures** (timeouts, dropped connections, `5xx`, rate limiting)
  are retried with a growing delay, up to `--retries`. Authentication failures,
  missing products and integrity problems are reported immediately and are never
  retried or counted as success.
* A short transfer, an unexpected size, or an HTML page returned in place of
  data all cause the file to be rejected rather than saved.

Optional MD5 verification is available with `--checksums`, using `md5sum`
format:

```
d41d8cd98f00b204e9800998ecf8427e  S1A_IW_SLC__1SDV_....zip
```

The checksum is verified before the file is renamed into place; a mismatch
quarantines it as `<name>.corrupt`. ASF does not publish MD5s through the
download URLs, so this applies only when you have obtained checksums separately.

URLs printed to the terminal have their query strings redacted, since ASF's
redirects carry a temporary download signature there.

---

## Known limitations

* Downloads are sequential.
* A response served as `text/html` is always rejected, so this tool cannot be
  used to fetch HTML documents. This is what stops an expired session from being
  saved under a product name.
* If a server reports no content length, the download is refused rather than
  accepted unverified.
* Resume depends on the server honouring HTTP range requests. ASF data endpoints
  normally support range requests; if an intermediary does not, the file simply
  restarts from zero.
* The `.py` reader targets the structure ASF Vertex currently generates. If that
  format changes, the tool will decline the file rather than guess at it.
* A stored session can expire during a long transfer; that surfaces as an
  authentication error, not as a truncated file.

---



## Notes

Please ensure compliance with ASF and NASA Earthdata terms of service. Users are
responsible for ensuring that their use of downloaded data complies with the
applicable terms and access requirements.

ASF's official download tools remain a good default when they meet your needs.



## License

MIT License. See [LICENSE](LICENSE).



## Author's Info

**Y-W. Chen**
- E-mail: mkcchenyaowen@gmail.com
- ORCID: https://orcid.org/0000-0002-1290-001X
- First release: December 23, 2024
- Version 2 update: September 5, 2026
