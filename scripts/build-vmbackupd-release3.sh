#!/usr/bin/env bash
set -Eeuo pipefail

cd /home/ilyamus/src/vmbackupd

echo "============================================================"
echo " vmbackupd Release 3 build"
echo "============================================================"

echo
echo "===== 1. SOURCE CONTRACT ====="
echo "Repository sources/tests are authoritative; build performs no source rewriting."
echo

echo "===== 2. SYNTAX ====="

.venv/bin/python -m compileall -q src

node --check cockpit/vmbackupd/api.js
node --check cockpit/vmbackupd/vmbackupd.js

bash -n packaging/receiver/vmbackupd-receiver-hostkey

git diff --check

echo "Syntax: PASS"

echo
echo "===== 3. RELEASE 3 CONTRACT ====="

grep -nE '^Name:|^Version:|^Release:' packaging/vmbackupd.spec

grep -q '^Release:        3%{?dist}$' packaging/vmbackupd.spec || {
    echo "ERROR: spec is not Release 3"
    exit 1
}

echo
echo "===== 4. COCKPIT / RECEIVER TESTS ====="

.venv/bin/python -m pytest -q \
    tests/test_cockpit_receiver.py \
    tests/test_cockpit_ssh_setup.py \
    tests/test_cockpit_ssh_storage.py \
    --tb=short

echo
echo "===== 5. BACKEND SSH TESTS ====="

.venv/bin/python -m pytest -q \
    tests/test_ssh_api_config.py \
    tests/test_ssh_receiver.py \
    tests/test_receiver_os_integration.py \
    tests/test_ssh_identity.py \
    tests/test_ssh_known_hosts.py \
    --tb=short

echo
echo "===== 6. PACKAGING TESTS ====="

.venv/bin/python -m pytest -q \
    tests/test_packaging.py \
    --tb=short

echo
echo "===== 7. FULL PYTEST ====="

.venv/bin/python -m pytest -q --tb=short

echo
echo "===== 8. STAGE RELEASE 3 ====="

git add \
    cockpit/vmbackupd/api.js \
    cockpit/vmbackupd/index.html \
    cockpit/vmbackupd/vmbackupd.js \
    packaging/receiver/vmbackupd-receiver-hostkey \
    packaging/vmbackupd.spec \
    src/vmbackupd/application.py \
    tests/test_packaging.py \
    tests/test_ssh_api_config.py \
    tests/test_cockpit_receiver.py \
    tests/test_cockpit_ssh_storage.py

git diff --cached --check

echo
echo "===== STAGED FILES ====="
git diff --cached --name-status

echo
echo "===== DIST MUST NOT BE STAGED ====="

if git diff --cached --name-only | grep -q '^dist/'; then
    echo "ERROR: dist/ is staged"
    exit 1
fi

echo "dist/: NOT STAGED"

echo
echo "===== UNSTAGED TRACKED FILES ====="

if [[ -n "$(git diff --name-only)" ]]; then
    echo "ERROR: there are unstaged tracked changes:"
    git diff --name-status
    exit 1
fi

echo "None"

echo
echo "===== 9. BUILD UNIFIED RPM ====="

bash packaging/build-rpm.sh

echo
echo "===== 10. FIND RELEASE 3 ARTIFACTS ====="

mapfile -t BIN_RPMS < <(
    find dist/rpm \
        -maxdepth 1 \
        -type f \
        -name 'vmbackupd-0.1.0-3.fc*.noarch.rpm' \
        | sort
)

mapfile -t SRC_RPMS < <(
    find dist/rpm \
        -maxdepth 1 \
        -type f \
        -name 'vmbackupd-0.1.0-3.fc*.src.rpm' \
        | sort
)

if [[ ${#BIN_RPMS[@]} -ne 1 ]]; then
    echo "ERROR: expected exactly one Release 3 binary RPM"
    printf '%s\n' "${BIN_RPMS[@]:-NONE}"
    exit 1
fi

if [[ ${#SRC_RPMS[@]} -ne 1 ]]; then
    echo "ERROR: expected exactly one Release 3 SRPM"
    printf '%s\n' "${SRC_RPMS[@]:-NONE}"
    exit 1
fi

BIN_RPM="${BIN_RPMS[0]}"
SRC_RPM="${SRC_RPMS[0]}"

echo "Binary RPM: $BIN_RPM"
echo "Source RPM: $SRC_RPM"

echo
echo "===== 11. SPLIT RELEASE 3 MUST NOT EXIST ====="

SPLIT="$(
    find dist/rpm \
        -maxdepth 1 \
        -type f \
        \( \
            -name 'vmbackupd-receiver-0.1.0-3*.rpm' \
            -o -name 'cockpit-vmbackupd-0.1.0-3*.rpm' \
        \) \
        -print
)"

if [[ -n "$SPLIT" ]]; then
    echo "ERROR: split Release 3 RPM found:"
    echo "$SPLIT"
    exit 1
fi

echo "OK: unified RPM only"

echo
echo "===== 12. UNIFIED PAYLOAD ====="

rpm -qlp "$BIN_RPM" | grep -E \
    'cockpit/vmbackupd|receiver_sshd|vmbackupd-receiver|vmbackupd-transfer|vmbackupd-authorized|receiver_sshd_config'

echo
echo "===== 13. PROVIDES ====="
rpm -qp --provides "$BIN_RPM" | grep -E \
    '(^| )vmbackupd|cockpit-vmbackupd'

echo
echo "===== 14. OBSOLETES ====="
rpm -qp --obsoletes "$BIN_RPM"

echo
echo "===== 15. CHECKSUMS ====="
sha256sum "$BIN_RPM" "$SRC_RPM"

echo
echo "============================================================"
echo " BUILD SUCCESS"
echo "============================================================"
echo
echo "Installable RPM:"
echo "  $BIN_RPM"
echo
echo "SRPM for Fedora 44:"
echo "  $SRC_RPM"
echo
echo "Git status:"
git status --short
