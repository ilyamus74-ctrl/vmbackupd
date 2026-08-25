%{!?upstream_version:%global upstream_version 0.1.3}

Name:           vmbackupd
Version:        %{upstream_version}
Release:        1%{?dist}
Summary:        Local KVM/libvirt backup management daemon
License:        GPL-3.0-or-later
URL:            https://github.com/ilyamus74-ctrl/vmbackupd
Source0:        %{url}/archive/refs/tags/v%{version}/%{name}-%{version}.tar.gz
Source1:        vmbackupd.service
Source2:        vmbackupd.sysusers
Source3:        vmbackupd.tmpfiles
Source4:        vmbackupd.toml
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros
Requires:       python3
Requires:       openssh-clients
Requires:       openssh-server
Requires:       cockpit-bridge >= 215
Requires:       /usr/bin/virsh
Requires:       qemu-img
Requires:       systemd
Requires:       acl
Requires:       polkit
Requires(pre):  systemd
Requires(pre):  shadow-utils
Requires(pre):  glibc-common
Requires(pre):  util-linux
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

Provides:       vmbackupd-receiver = %{version}-%{release}
Obsoletes:      vmbackupd-receiver < %{version}-%{release}
Provides:       cockpit-vmbackupd = %{version}-%{release}
Obsoletes:      cockpit-vmbackupd < %{version}-%{release}

%description
vmbackupd is a persistent local daemon and UNIX-socket control plane for
conservative KVM/libvirt backup orchestration. The package includes the
vmbackupctl console client and the Cockpit web frontend for managing
backups, storage destinations, replication, and restore operations.

%prep
%autosetup -p1

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%check
PYTHONPATH=src %{python3} -m compileall -q src/vmbackupd
PYTHONPATH=src %{python3} -c "import vmbackupd"
PYTHONPATH=src %{python3} -c "from vmbackupd.daemon import main"
PYTHONPATH=src %{python3} -c "from vmbackupd.cli import main"

%install
%pyproject_install
%pyproject_save_files vmbackupd
install -Dpm 0644 docs/man/vmbackupd.8 \
    %{buildroot}%{_mandir}/man8/vmbackupd.8
install -Dpm 0644 docs/man/vmbackupctl.1 \
    %{buildroot}%{_mandir}/man1/vmbackupctl.1
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_unitdir}/vmbackupd.service
install -Dpm 0644 %{SOURCE2} %{buildroot}%{_sysusersdir}/vmbackupd.conf
install -Dpm 0644 %{SOURCE3} %{buildroot}%{_tmpfilesdir}/vmbackupd.conf
install -Dpm 0644 %{SOURCE4} %{buildroot}%{_sysconfdir}/vmbackupd/vmbackupd.toml
install -Dpm 0644 packaging/vmbackupd-libvirt.rules \
    %{buildroot}%{_datadir}/polkit-1/rules.d/60-vmbackupd-libvirt.rules

install -Dpm 0755 packaging/vmbackupd-storage-helper \
    %{buildroot}%{_libexecdir}/vmbackupd-storage-helper
install -Dpm 0755 packaging/vmbackupd-cockpit-helper \
    %{buildroot}%{_libexecdir}/vmbackupd-cockpit-helper
install -Dpm 0644 packaging/vmbackupd-storage-helper.socket \
    %{buildroot}%{_unitdir}/vmbackupd-storage-helper.socket
install -Dpm 0644 packaging/vmbackupd-storage-helper@.service \
    %{buildroot}%{_unitdir}/vmbackupd-storage-helper@.service
install -d -m 0755 %{buildroot}%{_datadir}/cockpit/vmbackupd
install -pm 0644 cockpit/vmbackupd/{manifest.json,index.html,api.js,model.js,views.js,main.js,vmbackupd.js,vmbackupd.css} \
    %{buildroot}%{_datadir}/cockpit/vmbackupd/

install -Dpm 0755 packaging/receiver/vmbackupd-authorized-keys \
    %{buildroot}%{_libexecdir}/vmbackupd-authorized-keys
install -Dpm 0755 packaging/receiver/vmbackupd-transfer-shell \
    %{buildroot}%{_libexecdir}/vmbackupd-transfer-shell
install -Dpm 0755 packaging/receiver/vmbackupd-receiver-session \
    %{buildroot}%{_libexecdir}/vmbackupd-receiver-session
install -Dpm 0755 packaging/receiver/vmbackupd-receiver-catalog \
    %{buildroot}%{_libexecdir}/vmbackupd-receiver-catalog
install -Dpm 0755 packaging/receiver/vmbackupd-receiver-resolver \
    %{buildroot}%{_libexecdir}/vmbackupd-receiver-resolver
install -Dpm 0755 packaging/receiver/vmbackupd-receiver-hostkey \
    %{buildroot}%{_libexecdir}/vmbackupd-receiver-hostkey

install -Dpm 0644 packaging/receiver/vmbackupd-receiver.sysusers \
    %{buildroot}%{_sysusersdir}/vmbackupd-receiver.conf
install -Dpm 0644 packaging/receiver/vmbackupd-receiver.tmpfiles \
    %{buildroot}%{_tmpfilesdir}/vmbackupd-receiver.conf
install -Dpm 0644 packaging/receiver/receiver_sshd_config \
    %{buildroot}%{_sysconfdir}/vmbackupd/receiver_sshd_config
install -Dpm 0644 packaging/receiver/vmbackupd-receiver-sshd.service \
    %{buildroot}%{_unitdir}/vmbackupd-receiver-sshd.service
install -Dpm 0644 packaging/receiver/vmbackupd-receiver-catalog.socket \
    %{buildroot}%{_unitdir}/vmbackupd-receiver-catalog.socket
install -Dpm 0644 packaging/receiver/vmbackupd-receiver-catalog@.service \
    %{buildroot}%{_unitdir}/vmbackupd-receiver-catalog@.service
install -Dpm 0644 packaging/receiver/vmbackupd-receiver-resolver.socket \
    %{buildroot}%{_unitdir}/vmbackupd-receiver-resolver.socket
install -Dpm 0644 packaging/receiver/vmbackupd-receiver-resolver@.service \
    %{buildroot}%{_unitdir}/vmbackupd-receiver-resolver@.service

%pre
%sysusers_create_compat %{SOURCE2}
%sysusers_create_compat packaging/receiver/vmbackupd-receiver.sysusers

%post
%systemd_post vmbackupd.service
%systemd_post vmbackupd-storage-helper.socket
%systemd_post vmbackupd-receiver-sshd.service
%systemd_post vmbackupd-receiver-catalog.socket
%systemd_post vmbackupd-receiver-resolver.socket

%tmpfiles_create %{_tmpfilesdir}/vmbackupd.conf
%tmpfiles_create %{_tmpfilesdir}/vmbackupd-receiver.conf

%preun
%systemd_preun vmbackupd.service
%systemd_preun vmbackupd-storage-helper.socket
%systemd_preun vmbackupd-receiver-sshd.service
%systemd_preun vmbackupd-receiver-catalog.socket
%systemd_preun vmbackupd-receiver-resolver.socket

%postun
%systemd_postun vmbackupd.service
%systemd_postun vmbackupd-storage-helper.socket
%systemd_postun vmbackupd-receiver-sshd.service
%systemd_postun vmbackupd-receiver-catalog.socket
%systemd_postun vmbackupd-receiver-resolver.socket

%files -f %{pyproject_files}
%license LICENSE
%doc docs/*.md

%{_bindir}/vmbackupd
%{_bindir}/vmbackupctl

%{_mandir}/man8/vmbackupd.8*
%{_mandir}/man1/vmbackupctl.1*

%dir %{_sysconfdir}/vmbackupd
%config(noreplace) %{_sysconfdir}/vmbackupd/vmbackupd.toml
%config(noreplace) %{_sysconfdir}/vmbackupd/receiver_sshd_config

%ghost %attr(2750,vmbackupd,vmbackupd-admin) %dir /run/vmbackupd
%ghost %attr(0750,vmbackupd,vmbackupd) %dir /var/lib/vmbackupd
%ghost %attr(0750,vmbackupd,vmbackupd) %dir /var/lib/vmbackupd/control
%ghost %attr(0700,vmbackupd,vmbackupd) %dir /var/lib/vmbackupd/ssh
%ghost %attr(0700,vmbackupd,vmbackupd) %dir /var/lib/vmbackupd/ssh/identities
%ghost %attr(0700,vmbackupd,vmbackupd) %dir /var/lib/vmbackupd/receiver
%ghost %attr(0700,root,root) %dir /var/lib/vmbackupd/receiver-host
%ghost %attr(0750,vmbackupd,qemu) %dir /var/lib/libvirt/images/vmbackupd
%ghost %attr(0750,vmbackupd-transfer,vmbackupd-transfer) %dir /srv/vmbackupd

%{_datadir}/polkit-1/rules.d/60-vmbackupd-libvirt.rules

%{_unitdir}/vmbackupd.service
%{_unitdir}/vmbackupd-storage-helper.socket
%{_unitdir}/vmbackupd-storage-helper@.service
%{_unitdir}/vmbackupd-receiver-sshd.service
%{_unitdir}/vmbackupd-receiver-catalog.socket
%{_unitdir}/vmbackupd-receiver-catalog@.service
%{_unitdir}/vmbackupd-receiver-resolver.socket
%{_unitdir}/vmbackupd-receiver-resolver@.service

%{_sysusersdir}/vmbackupd.conf
%{_sysusersdir}/vmbackupd-receiver.conf

%{_tmpfilesdir}/vmbackupd.conf
%{_tmpfilesdir}/vmbackupd-receiver.conf

%{_libexecdir}/vmbackupd-storage-helper
%{_libexecdir}/vmbackupd-cockpit-helper
%{_libexecdir}/vmbackupd-authorized-keys
%{_libexecdir}/vmbackupd-transfer-shell
%{_libexecdir}/vmbackupd-receiver-session
%{_libexecdir}/vmbackupd-receiver-catalog
%{_libexecdir}/vmbackupd-receiver-resolver
%{_libexecdir}/vmbackupd-receiver-hostkey

%{_datadir}/cockpit/vmbackupd/

%changelog
* Tue Aug 25 2026 Illia Chykalov <ilyamus74@gmail.com> - 0.1.3-1
- Fix Python license metadata for Fedora setuptools compatibility

* Tue Aug 25 2026 Illia Chykalov <ilyamus74@gmail.com> - 0.1.2-1
- Add manual pages for vmbackupd and vmbackupctl
- Add project and maintainer metadata
- Clean up Fedora RPM packaging

* Tue Aug 25 2026 Illia Chykalov <ilyamus74@gmail.com> - 0.1.1-1
- Release vmbackupd 0.1.1
- License the project under GPL-3.0-or-later
- Publish upstream sources on GitHub
- Include the Stage49 received-backup delete deadlock fix

* Thu Aug 20 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-7
- Abort rejected PLANNED reclaim operations before destructive work begins
- Prevent replica, policy, or snapshot safety refusals from leaving stale reclaim operations
- Preserve RECOVERY_REQUIRED handling for failures after destructive reclaim starts

* Thu Aug 20 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-6
- Make Full backups to retain authoritative for FULL-only jobs
- Keep restore-point retention semantics for incremental chains
- Clarify FULL retention controls in Cockpit

* Thu Aug 20 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-5
- Add schema version 18 with separate CAPACITY and RETENTION reclaim purposes
- Add durable post-success retention reclaim through the existing safe reclaim pipeline
- Fail closed when replica locations or replica tasks depend on reclaim candidates
- Add restart catch-up for interrupted post-success retention
- Require explicit recovery for interrupted destructive retention
- Normalize stale recovery diagnostics on completed v17 reclaim migration

* Thu Aug 20 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-4
- Update runtime and database support through schema version 17
- Fix reclaim recovery blocked by terminal historical job runs
- Preserve destructive reclaim recovery after later policy changes
- Clear stale reclaim errors after successful completion
- Improve capacity inspection diagnostics

* Tue Aug 18 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-3
- Add Cockpit SSH receiver authorization controls
- Manage SSH staging paths automatically

* Tue Aug 18 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-2
- Merge daemon, Cockpit frontend, and SSH receiver into one RPM

* Mon Aug 17 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-1
- Initial Fedora-style package
