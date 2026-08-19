%{!?upstream_version:%global upstream_version 0.1.0}

Name:           vmbackupd
Version:        %{upstream_version}
Release:        3%{?dist}
Summary:        Local KVM/libvirt backup management daemon
License:        LicenseRef-Proprietary
Source0:        %{name}-%{version}.tar.gz
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
Requires:       libvirt-client
Requires:       qemu-img
Requires:       libvirt-daemon-driver-qemu
Requires:       systemd
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
vmbackupctl console client. The optional Cockpit frontend is shipped as the
separate cockpit-vmbackupd binary package from this source build.

%prep
%autosetup -p1

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files vmbackupd
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_unitdir}/vmbackupd.service
install -Dpm 0644 %{SOURCE2} %{buildroot}%{_sysusersdir}/vmbackupd.conf
install -Dpm 0644 %{SOURCE3} %{buildroot}%{_tmpfilesdir}/vmbackupd.conf
install -Dpm 0644 %{SOURCE4} %{buildroot}%{_sysconfdir}/vmbackupd/vmbackupd.toml
install -d -m 0755 %{buildroot}%{_datadir}/cockpit/vmbackupd
install -pm 0644 cockpit/vmbackupd/{manifest.json,index.html,api.js,vmbackupd.js,vmbackupd.css} \
    %{buildroot}%{_datadir}/cockpit/vmbackupd/

install -Dpm 0755 packaging/receiver/vmbackupd-authorized-keys \
    %{buildroot}%{_libexecdir}/vmbackupd-authorized-keys
install -Dpm 0755 packaging/receiver/vmbackupd-transfer-shell \
    %{buildroot}%{_libexecdir}/vmbackupd-transfer-shell
install -Dpm 0755 packaging/receiver/vmbackupd-receiver-session \
    %{buildroot}%{_libexecdir}/vmbackupd-receiver-session
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

%pre
%sysusers_create_compat %{SOURCE2}
%sysusers_create_compat packaging/receiver/vmbackupd-receiver.sysusers

%post
%systemd_post vmbackupd.service
%systemd_post vmbackupd-receiver-sshd.service

%tmpfiles_create %{_tmpfilesdir}/vmbackupd.conf
%tmpfiles_create %{_tmpfilesdir}/vmbackupd-receiver.conf

%preun
%systemd_preun vmbackupd.service
%systemd_preun vmbackupd-receiver-sshd.service

%postun
%systemd_postun vmbackupd.service
%systemd_postun vmbackupd-receiver-sshd.service

%files -f %{pyproject_files}
%doc docs/*.md

%{_bindir}/vmbackupd
%{_bindir}/vmbackupctl

%config(noreplace) %{_sysconfdir}/vmbackupd/vmbackupd.toml
%config(noreplace) %{_sysconfdir}/vmbackupd/receiver_sshd_config

%{_unitdir}/vmbackupd.service
%{_unitdir}/vmbackupd-receiver-sshd.service

%{_sysusersdir}/vmbackupd.conf
%{_sysusersdir}/vmbackupd-receiver.conf

%{_tmpfilesdir}/vmbackupd.conf
%{_tmpfilesdir}/vmbackupd-receiver.conf

%{_libexecdir}/vmbackupd-authorized-keys
%{_libexecdir}/vmbackupd-transfer-shell
%{_libexecdir}/vmbackupd-receiver-session
%{_libexecdir}/vmbackupd-receiver-hostkey

%{_datadir}/cockpit/vmbackupd/

%changelog
* Tue Aug 18 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-3
- Add Cockpit SSH receiver authorization controls
- Manage SSH staging paths automatically

* Tue Aug 18 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-2
- Merge daemon, Cockpit frontend, and SSH receiver into one RPM

* Mon Aug 17 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-1
- Initial Fedora-style package
