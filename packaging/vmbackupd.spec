%{!?upstream_version:%global upstream_version 0.1.0}

Name:           vmbackupd
Version:        %{upstream_version}
Release:        1%{?dist}
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

%description
vmbackupd is a persistent local daemon and UNIX-socket control plane for
conservative KVM/libvirt backup orchestration. The package includes the
vmbackupctl console client. It does not include Cockpit integration.

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

%pre
%sysusers_create_compat %{SOURCE2}

%post
%systemd_post vmbackupd.service
%tmpfiles_create %{_tmpfilesdir}/vmbackupd.conf

%preun
%systemd_preun vmbackupd.service

%postun
%systemd_postun vmbackupd.service

%files -f %{pyproject_files}
%doc docs/*.md
%{_bindir}/vmbackupd
%{_bindir}/vmbackupctl
%config(noreplace) %{_sysconfdir}/vmbackupd/vmbackupd.toml
%{_unitdir}/vmbackupd.service
%{_sysusersdir}/vmbackupd.conf
%{_tmpfilesdir}/vmbackupd.conf

%changelog
* Mon Aug 17 2026 vmbackupd packagers <packagers@example.invalid> - 0.1.0-1
- Initial Fedora-style package
