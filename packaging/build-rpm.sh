#!/usr/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
cd -- "$repo_root"
source_date_epoch=$(git -C "$repo_root" log -1 --format=%ct)
work_root=${RPMBUILD_WORK_ROOT:-$(mktemp -d)}
keep_work_root=${RPMBUILD_KEEP_WORK_ROOT:-0}
output_dir=${RPMBUILD_OUTPUT_DIR:-$repo_root/dist/rpm}

cleanup() {
    if [[ "$keep_work_root" != 1 ]]; then
        rm -rf -- "$work_root"
    fi
}
trap cleanup EXIT

if ! command -v rpmbuild >/dev/null 2>&1; then
    echo "rpmbuild is required; install Fedora RPM build tooling first" >&2
    exit 2
fi

snapshot_root="$work_root/index-snapshot"
mkdir -p "$snapshot_root" "$work_root/rpmbuild"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS} "$output_dir"
git -C "$repo_root" checkout-index --all --force --prefix="$snapshot_root/"
version=$(python3 -c 'import pathlib,sys,tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["project"]["version"])' "$snapshot_root/pyproject.toml")
manifest="$work_root/source-files"
git -C "$repo_root" ls-files --cached \
    | grep -Ev '^(\.git|\.venv|build|dist|tests)/' \
    | LC_ALL=C sort > "$manifest"

tar --create --gzip --file "$work_root/rpmbuild/SOURCES/vmbackupd-$version.tar.gz" \
    --directory "$snapshot_root" --files-from "$manifest" --sort=name \
    --mtime="@$source_date_epoch" --owner=0 --group=0 --numeric-owner \
    --transform="s,^,vmbackupd-$version/,"
install -pm 0644 "$snapshot_root/packaging"/vmbackupd.{service,sysusers,tmpfiles,toml} \
    "$work_root/rpmbuild/SOURCES/"
install -pm 0644 "$snapshot_root/packaging/vmbackupd.spec" "$work_root/rpmbuild/SPECS/"

rpmbuild -ba "$work_root/rpmbuild/SPECS/vmbackupd.spec" \
    --define "_topdir $work_root/rpmbuild" \
    --define "upstream_version $version" \
    --define "_source_date_epoch $source_date_epoch"

find "$work_root/rpmbuild/RPMS" "$work_root/rpmbuild/SRPMS" \
    -type f \( -name '*.rpm' -o -name '*.src.rpm' \) -exec cp -p {} "$output_dir/" \;
find "$output_dir" -maxdepth 1 -type f \
    \( -name 'vmbackupd-*.rpm' -o -name 'cockpit-vmbackupd-*.rpm' \) \
    -print | LC_ALL=C sort
