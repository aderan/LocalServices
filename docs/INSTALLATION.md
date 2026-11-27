# Locove installation, updates, and removal

The preview builds the menu bar app on your Mac. See the [installation requirements](../README.md#install). If you downloaded the source archive and its checksum, verify the archive before extracting it. For 0.5.1, run `shasum -a 256 -c locove-0.5.1.tar.gz.sha256` from the download directory.

To update, replace the source in the same directory and rerun `bash scripts/install.sh`. To remove Locove, run `bash scripts/uninstall.sh`. Add `--purge` to remove Locove's settings, registrations, and logs as well. Your project files are kept.

Existing registrations and language choices carry over. If you used a development checkout with bundled private manifests, move the TOML files you still use into `~/Library/Application Support/LocalServices/builtin-services/` before updating.

For a bug report, include your Locove version, macOS version, Mac architecture, and a redacted error or log.

If you added a proxy exception for optional local domains, you can undo it in macOS System Settings. Open the active network service's proxy settings and remove the entry Locove added, such as `*.locove`, from the bypass list.

## Publish a source release

Run **Source release** from the GitHub Actions tab to preview the archive. The workflow uploads the archive and checksum to the run's **Artifacts**.

Update the version in the daemon and menu bar app and add an English entry to [CHANGELOG.md](../CHANGELOG.md). Push a version tag such as `v0.5.1` from the tested commit. The tag run publishes the matching changelog entry, source archive, and checksum on GitHub Releases.
