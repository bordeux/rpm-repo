#!/usr/bin/env python3
"""
RPM Repository Generator

Generates an RPM repository from GitHub releases containing .rpm packages.
Similar to apt-repo but for Fedora/RHEL/CentOS systems.

Supports incremental updates - can update a single project while preserving others.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml


# Architecture patterns for .rpm files
ARCH_PATTERNS = {
    "x86_64": [r"x86_64", r"amd64", r"x64"],
    "aarch64": [r"aarch64", r"arm64"],
    "i686": [r"i686", r"i386", r"x86[^_]"],
    "armv7hl": [r"armv7hl", r"armhf"],
    "noarch": [r"noarch"],
}

# Manifest file to track packages by project
MANIFEST_FILE = "packages.json"


@dataclass
class RpmPackage:
    """Represents an .rpm package from a GitHub release."""
    name: str
    version: str
    architecture: str
    url: str
    filename: str
    project_repo: str = ""
    size: int = 0
    sha256: str = ""
    # Extracted from .rpm
    summary: str = ""
    description: str = ""
    license: str = ""
    vendor: str = ""
    homepage: str = ""


@dataclass
class Release:
    """Represents a GitHub release."""
    tag: str
    version: str
    major_minor: str
    packages: list[RpmPackage] = field(default_factory=list)


@dataclass
class Project:
    """Project configuration from projects.yaml."""
    repo: str
    name: str = ""
    description: str = ""
    keep_versions: int = 0
    asset_pattern: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = self.repo.split("/")[-1]


@dataclass
class RepoSettings:
    """Repository settings from projects.yaml."""
    name: str = "github-packages"
    baseurl: str = ""
    architectures: list[str] = field(default_factory=lambda: ["x86_64", "aarch64"])
    description: str = "GitHub Packages"
    sign_packages: bool = True


class GitHubAPI:
    """GitHub API client for fetching releases."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.base_url = "https://api.github.com"

    def _request(self, endpoint: str) -> dict | list:
        """Make an authenticated request to GitHub API."""
        url = f"{self.base_url}/{endpoint}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "rpm-repo-generator",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode())
        except HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"Repository or release not found: {endpoint}")
            elif e.code == 403:
                raise RuntimeError(
                    f"Rate limit exceeded. Set GITHUB_TOKEN env var for higher limits."
                )
            raise

    def get_repo(self, repo: str) -> dict:
        """Get repository metadata."""
        return self._request(f"repos/{repo}")

    def get_releases(self, repo: str, per_page: int = 30) -> list[dict]:
        """Get releases for a repository."""
        return self._request(f"repos/{repo}/releases?per_page={per_page}")

    def get_latest_release(self, repo: str) -> dict:
        """Get the latest release."""
        return self._request(f"repos/{repo}/releases/latest")


def extract_version(tag: str) -> str:
    """Extract version number from tag (removes 'v' prefix)."""
    return tag.lstrip("vV")


def extract_major_minor(version: str) -> str:
    """Extract major.minor from version string."""
    parts = version.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return version


def detect_architecture(filename: str) -> Optional[str]:
    """Detect architecture from .rpm filename."""
    filename_lower = filename.lower()
    for arch, patterns in ARCH_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, filename_lower):
                return arch
    return None


def compute_sha256(filepath: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()


def download_file(url: str, dest: Path, token: Optional[str] = None) -> None:
    """Download a file from URL to destination."""
    headers = {"User-Agent": "rpm-repo-generator"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(url, headers=headers)
    with urlopen(req, timeout=300) as response:
        with open(dest, "wb") as f:
            shutil.copyfileobj(response, f)


def extract_rpm_info(rpm_path: Path) -> dict:
    """Extract information from an .rpm file."""
    info = {}
    try:
        # Use rpm command to query package info
        result = subprocess.run(
            [
                "rpm", "-qip", str(rpm_path),
                "--queryformat",
                "%{NAME}\\n%{VERSION}\\n%{RELEASE}\\n%{SUMMARY}\\n%{LICENSE}\\n%{VENDOR}\\n%{URL}\\n%{DESCRIPTION}"
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            lines = result.stdout.split("\n")
            if len(lines) >= 7:
                info["name"] = lines[0] if lines[0] != "(none)" else ""
                info["version"] = lines[1] if lines[1] != "(none)" else ""
                info["release"] = lines[2] if lines[2] != "(none)" else ""
                info["summary"] = lines[3] if lines[3] != "(none)" else ""
                info["license"] = lines[4] if lines[4] != "(none)" else ""
                info["vendor"] = lines[5] if lines[5] != "(none)" else ""
                info["url"] = lines[6] if lines[6] != "(none)" else ""
                info["description"] = "\n".join(lines[7:]) if len(lines) > 7 else ""
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return info


def find_rpm_assets(
    release_data: dict,
    project: Project,
    architectures: list[str],
) -> list[dict]:
    """Find .rpm assets in a release matching the project configuration."""
    assets = []

    for asset in release_data.get("assets", []):
        name = asset.get("name", "")

        # Must be an .rpm file
        if not name.endswith(".rpm"):
            continue

        # Skip source RPMs
        if ".src.rpm" in name or ".srpm" in name:
            continue

        # Apply custom pattern filter if specified
        if project.asset_pattern:
            if not re.search(project.asset_pattern, name, re.IGNORECASE):
                continue

        # Detect architecture
        arch = detect_architecture(name)
        if arch and arch in architectures:
            assets.append({
                "name": name,
                "url": asset.get("browser_download_url", ""),
                "size": asset.get("size", 0),
                "architecture": arch,
            })

    return assets


def fetch_releases(
    github: GitHubAPI,
    project: Project,
    settings: RepoSettings,
) -> list[Release]:
    """Fetch and process releases for a project."""
    releases_data = github.get_releases(project.repo)

    # Fetch description from GitHub if not provided
    description = project.description
    if not description:
        try:
            repo_info = github.get_repo(project.repo)
            description = repo_info.get("description") or f"{project.name} from GitHub"
        except Exception:
            description = f"{project.name} from GitHub"

    # Group releases by major.minor version
    releases_by_minor: dict[str, dict] = {}

    for release_data in releases_data:
        # Skip pre-releases and drafts
        if release_data.get("prerelease") or release_data.get("draft"):
            continue

        tag = release_data["tag_name"]
        version = extract_version(tag)
        major_minor = extract_major_minor(version)

        # Keep only the first (latest) release for each major.minor
        if major_minor not in releases_by_minor:
            releases_by_minor[major_minor] = release_data

    # Sort by version (newest first)
    sorted_versions = sorted(
        releases_by_minor.keys(),
        key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")],
        reverse=True,
    )

    # Determine how many versions to keep
    if project.keep_versions > 0:
        versions_to_keep = sorted_versions[: project.keep_versions + 1]
    else:
        versions_to_keep = sorted_versions[:1]  # Only latest

    releases = []
    for major_minor in versions_to_keep:
        release_data = releases_by_minor[major_minor]
        tag = release_data["tag_name"]
        version = extract_version(tag)

        # Find .rpm assets
        rpm_assets = find_rpm_assets(release_data, project, settings.architectures)

        if rpm_assets:
            release = Release(
                tag=tag,
                version=version,
                major_minor=major_minor,
            )

            for asset in rpm_assets:
                package = RpmPackage(
                    name=project.name,
                    version=version,
                    architecture=asset["architecture"],
                    url=asset["url"],
                    filename=asset["name"],
                    size=asset["size"],
                    project_repo=project.repo,
                    summary=description,
                    homepage=f"https://github.com/{project.repo}",
                )
                release.packages.append(package)

            releases.append(release)

    return releases


def load_manifest(output_dir: Path) -> dict[str, list[dict]]:
    """Load existing packages manifest."""
    manifest_path = output_dir / MANIFEST_FILE
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)
    return {"packages": []}


def save_manifest(output_dir: Path, packages: list[RpmPackage]) -> None:
    """Save packages manifest."""
    manifest_path = output_dir / MANIFEST_FILE
    data = {
        "packages": [asdict(pkg) for pkg in packages],
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    with open(manifest_path, "w") as f:
        json.dump(data, f, indent=2)


def run_createrepo(packages_dir: Path) -> bool:
    """Run createrepo_c to generate repository metadata."""
    try:
        # Check if createrepo_c is available, fall back to createrepo
        createrepo_cmd = "createrepo_c"
        result = subprocess.run(
            ["which", createrepo_cmd],
            capture_output=True,
        )
        if result.returncode != 0:
            createrepo_cmd = "createrepo"

        # Run createrepo
        result = subprocess.run(
            [createrepo_cmd, "--update", str(packages_dir)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            print(f"  Successfully generated repository metadata")
            return True
        else:
            print(f"  Error running createrepo: {result.stderr}")
            return False
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"  createrepo not available: {e}")
        return False


def sign_repository(repodata_dir: Path, gpg_key: Optional[str] = None) -> bool:
    """Sign the repository metadata with GPG."""
    repomd_path = repodata_dir / "repomd.xml"
    if not repomd_path.exists():
        return False

    try:
        gpg_cmd = ["gpg", "--batch", "--yes", "--detach-sign", "--armor"]
        if gpg_key:
            gpg_cmd.extend(["--local-user", gpg_key])
        gpg_cmd.append(str(repomd_path))

        subprocess.run(gpg_cmd, check=True, capture_output=True, timeout=60)
        return True
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"  Warning: GPG signing failed: {e}")
        return False


def sign_rpm_package(rpm_path: Path, gpg_key: Optional[str] = None) -> bool:
    """Sign an RPM package with GPG using rpm --addsign."""
    try:
        # Create a temporary rpmmacros file for non-interactive signing
        rpmmacros = Path.home() / ".rpmmacros"
        macros_content = "%_gpg_name {}\n%__gpg_sign_cmd %{{__gpg}} gpg --batch --no-armor --no-secmem-warning -u \"%{{_gpg_name}}\" -sbo %{{__signature_filename}} %{{__plaintext_filename}}\n".format(
            gpg_key or ""
        )

        # Backup existing .rpmmacros if it exists
        backup_path = None
        if rpmmacros.exists():
            backup_path = rpmmacros.with_suffix(".rpmmacros.bak")
            shutil.copy2(rpmmacros, backup_path)

        try:
            rpmmacros.write_text(macros_content)

            # Sign the RPM package
            result = subprocess.run(
                ["rpm", "--addsign", str(rpm_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode == 0:
                return True
            else:
                print(f"      Warning: Failed to sign {rpm_path.name}: {result.stderr}")
                return False
        finally:
            # Restore original .rpmmacros
            if backup_path and backup_path.exists():
                shutil.move(backup_path, rpmmacros)
            elif rpmmacros.exists():
                rpmmacros.unlink()

    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"      Warning: RPM signing failed: {e}")
        return False


def export_public_key(output_path: Path, gpg_key: Optional[str] = None) -> bool:
    """Export GPG public key for repository users."""
    try:
        gpg_cmd = ["gpg", "--armor", "--export"]
        if gpg_key:
            gpg_cmd.append(gpg_key)

        result = subprocess.run(gpg_cmd, capture_output=True, timeout=60)
        if result.returncode == 0 and result.stdout:
            output_path.write_bytes(result.stdout)
            return True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return False


def generate_repo_file(
    output_path: Path,
    settings: RepoSettings,
    gpg_key: Optional[str] = None,
    sign_packages: bool = False,
) -> None:
    """Generate .repo file for easy installation."""
    # baseurl must point to packages/ where repodata/ lives
    packages_url = f"{settings.baseurl.rstrip('/')}/packages"

    if gpg_key:
        if sign_packages:
            # Both individual packages and repo metadata are signed
            gpg_lines = f"""gpgcheck=1
repo_gpgcheck=1
gpgkey={settings.baseurl}/RPM-GPG-KEY-{settings.name}"""
        else:
            # Only repo metadata is signed, not individual packages
            gpg_lines = f"""gpgcheck=0
repo_gpgcheck=1
gpgkey={settings.baseurl}/RPM-GPG-KEY-{settings.name}"""
    else:
        gpg_lines = "gpgcheck=0"

    content = f"""[{settings.name}]
name={settings.description}
baseurl={packages_url}
enabled=1
{gpg_lines}
"""
    output_path.write_text(content.strip() + "\n")


def load_config(config_path: Path) -> tuple[RepoSettings, list[Project]]:
    """Load configuration from projects.yaml."""
    with open(config_path) as f:
        data = yaml.safe_load(f)

    settings_data = data.get("settings", {})
    settings = RepoSettings(
        name=settings_data.get("name", "github-packages"),
        baseurl=settings_data.get("baseurl", ""),
        architectures=settings_data.get("architectures", ["x86_64", "aarch64"]),
        description=settings_data.get("description", "GitHub Packages"),
        sign_packages=settings_data.get("sign_packages", True),
    )

    projects = []
    for proj_data in data.get("projects", []):
        projects.append(Project(
            repo=proj_data["repo"],
            name=proj_data.get("name", ""),
            description=proj_data.get("description", ""),
            keep_versions=proj_data.get("keep_versions", 0),
            asset_pattern=proj_data.get("asset_pattern", ""),
        ))

    return settings, projects


def cleanup_old_packages(
    packages_dir: Path,
    all_packages: list[RpmPackage],
) -> list[Path]:
    """Remove .rpm files that are no longer in the manifest."""
    removed = []
    current_filenames = {pkg.filename for pkg in all_packages}

    if packages_dir.exists():
        for rpm_file in packages_dir.glob("*.rpm"):
            if rpm_file.name not in current_filenames:
                rpm_file.unlink()
                removed.append(rpm_file)

    return removed


def main():
    parser = argparse.ArgumentParser(
        description="Generate RPM repository from GitHub releases"
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=Path("projects.yaml"),
        help="Path to projects.yaml config file",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("repo"),
        help="Output directory for repository (usually gh-pages checkout)",
    )
    parser.add_argument(
        "--project", "-p",
        type=str,
        help="Process only specific project (name or owner/repo)",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List configured projects and exit",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be done without downloading",
    )
    parser.add_argument(
        "--gpg-key", "-k",
        type=str,
        help="GPG key ID for signing (optional)",
    )
    parser.add_argument(
        "--no-sign",
        action="store_true",
        help="Skip GPG signing",
    )

    args = parser.parse_args()

    # Load configuration
    settings, all_projects = load_config(args.config)

    # List mode
    if args.list:
        print("Configured projects:")
        for proj in all_projects:
            print(f"  - {proj.repo} (name: {proj.name}, keep_versions: {proj.keep_versions})")
        return

    # Determine which projects to process
    if args.project:
        projects_to_process = [
            p for p in all_projects
            if p.name == args.project or p.repo == args.project
        ]
        if not projects_to_process:
            print(f"Error: Project '{args.project}' not found in config")
            return 1
    else:
        projects_to_process = all_projects

    # Initialize GitHub API client
    github = GitHubAPI()

    output_dir = args.output
    packages_dir = output_dir / "packages"

    # Load existing manifest (for incremental updates)
    manifest = load_manifest(output_dir)
    existing_packages = [
        RpmPackage(**pkg_data) for pkg_data in manifest.get("packages", [])
    ]

    # Filter out packages from projects we're about to update
    projects_to_update = {p.repo for p in projects_to_process}
    preserved_packages = [
        pkg for pkg in existing_packages
        if pkg.project_repo not in projects_to_update
    ]

    if not args.dry_run:
        packages_dir.mkdir(parents=True, exist_ok=True)

    print(f"Processing {len(projects_to_process)} project(s)...")
    if preserved_packages:
        print(f"Preserving {len(preserved_packages)} package(s) from other projects")

    # Collect new packages
    new_packages: list[RpmPackage] = []

    for project in projects_to_process:
        print(f"\n{'='*60}")
        print(f"Project: {project.repo}")
        print(f"{'='*60}")

        try:
            releases = fetch_releases(github, project, settings)

            if not releases:
                print(f"  No releases with .rpm packages found")
                continue

            for release in releases:
                print(f"\n  Release: {release.tag} (version {release.version})")

                for pkg in release.packages:
                    print(f"    - {pkg.filename} ({pkg.architecture})")

                    if args.dry_run:
                        continue

                    # Download .rpm file
                    rpm_path = packages_dir / pkg.filename
                    needs_signing = False
                    if not rpm_path.exists():
                        print(f"      Downloading...")
                        download_file(pkg.url, rpm_path, github.token)
                        needs_signing = True
                    else:
                        print(f"      Already exists, skipping download")

                    # Sign RPM package if enabled in config
                    if settings.sign_packages and args.gpg_key and needs_signing:
                        print(f"      Signing...")
                        if sign_rpm_package(rpm_path, args.gpg_key):
                            print(f"      Signed successfully")

                    # Compute hash
                    pkg.sha256 = compute_sha256(rpm_path)
                    pkg.size = rpm_path.stat().st_size

                    # Extract info from .rpm
                    info = extract_rpm_info(rpm_path)
                    if info:
                        pkg.name = info.get("name", pkg.name)
                        pkg.summary = info.get("summary", pkg.summary)
                        pkg.description = info.get("description", pkg.description)
                        pkg.license = info.get("license", pkg.license)
                        pkg.vendor = info.get("vendor", pkg.vendor)
                        pkg.homepage = info.get("url", pkg.homepage)

                    new_packages.append(pkg)

        except Exception as e:
            print(f"  Error processing {project.repo}: {e}")
            continue

    if args.dry_run:
        print("\n[Dry run - no files written]")
        return

    # Combine preserved and new packages
    all_packages = preserved_packages + new_packages

    # Clean up old .rpm files no longer needed
    removed = cleanup_old_packages(packages_dir, all_packages)
    if removed:
        print(f"\nRemoved {len(removed)} old package(s)")

    # Save manifest
    save_manifest(output_dir, all_packages)

    print(f"\n{'='*60}")
    print("Generating repository metadata...")
    print(f"{'='*60}")

    # Run createrepo to generate metadata
    if not run_createrepo(packages_dir):
        print("  Warning: Could not generate repository metadata with createrepo")
        print("  Repository may not be usable without manual metadata generation")

    # Sign repository
    repodata_dir = packages_dir / "repodata"
    if not args.no_sign and repodata_dir.exists():
        if sign_repository(repodata_dir, args.gpg_key):
            print(f"  Created repomd.xml.asc (signed)")

            # Export public key
            pubkey_path = output_dir / f"RPM-GPG-KEY-{settings.name}"
            if export_public_key(pubkey_path, args.gpg_key):
                print(f"  Exported public key to RPM-GPG-KEY-{settings.name}")
        else:
            print(f"  Skipped signing (GPG not available or no key)")

    # Generate .repo file
    repo_file_path = output_dir / f"{settings.name}.repo"
    generate_repo_file(
        repo_file_path,
        settings,
        gpg_key=args.gpg_key if not args.no_sign else None,
        sign_packages=settings.sign_packages,
    )
    print(f"  Created {settings.name}.repo")

    print(f"\nRepository generated in: {output_dir}")
    print(f"Total packages: {len(all_packages)}")


if __name__ == "__main__":
    exit(main() or 0)
