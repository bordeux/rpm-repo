# RPM Repository Generator

Generate an RPM repository from GitHub releases containing `.rpm` packages. Host your own Fedora/RHEL/CentOS package repository on GitHub Pages.

## How It Works

1. Define projects in `projects.yaml` pointing to GitHub repositories that release `.rpm` files
2. GitHub Actions fetches releases, downloads `.rpm` packages, and generates repository metadata using `createrepo_c`
3. The repository is deployed to GitHub Pages
4. Users can add your repository to their system and install packages with `dnf` or `yum`

## Quick Start

### 1. Configure Projects

Edit `projects.yaml` to add GitHub repositories:

```yaml
settings:
  name: bordeux
  baseurl: https://bordeux.github.io/rpm-repo
  architectures:
    - x86_64
    - aarch64

projects:
  - repo: bordeux/tmpltool
    keep_versions: 1        # Keep 1 previous version
```

### 2. Enable GitHub Pages

1. Go to repository **Settings** > **Pages**
2. Set **Source** to "Deploy from a branch"
3. Select **gh-pages** branch

### 3. (Optional) Set Up GPG Signing

For signed repositories:

1. Generate a GPG key: `gpg --full-generate-key`
2. Export private key: `gpg --armor --export-secret-keys YOUR_KEY_ID`
3. Add as repository secret `GPG_PRIVATE_KEY`
4. Add repository variable `GPG_KEY_ID` with your key ID

### 4. Trigger the Workflow

Go to **Actions** > **Update RPM Repository** > **Run workflow**

## Using the Repository

Once deployed, users can add your repository:

```bash
# Download the .repo file
sudo curl -o /etc/yum.repos.d/bordeux.repo https://bordeux.github.io/rpm-repo/bordeux.repo

# Import GPG key (if signed)
sudo rpm --import https://bordeux.github.io/rpm-repo/RPM-GPG-KEY-bordeux

# Install packages
sudo dnf install tmpltool
```

Or manually:

```bash
sudo tee /etc/yum.repos.d/bordeux.repo <<EOF
[bordeux]
name=Bordeux Packages
baseurl=https://bordeux.github.io/rpm-repo/packages
enabled=1
gpgcheck=0
EOF

sudo dnf install tmpltool
```

## Configuration Reference

### projects.yaml

```yaml
settings:
  name: bordeux                           # Repository name
  baseurl: https://bordeux.github.io/rpm-repo  # Base URL
  architectures:                          # Supported architectures
    - x86_64
    - aarch64
  description: "Bordeux Packages"         # Repository description

projects:
  - repo: bordeux/tmpltool                # GitHub repository (required)
    name: tmpltool                        # Package name override
    description: "Description"            # Description override
    keep_versions: 1                      # Past versions to keep
    asset_pattern: ".*x86_64.*"           # Regex to filter .rpm assets
```

### Command Line

```bash
# Set up virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Generate repository locally (requires createrepo_c)
python scripts/generate_repo.py --output repo

# Process specific project
python scripts/generate_repo.py --project bordeux/tmpltool

# Dry run (no downloads)
python scripts/generate_repo.py --dry-run

# List configured projects
python scripts/generate_repo.py --list

# With GPG signing
python scripts/generate_repo.py --gpg-key YOUR_KEY_ID

# Skip signing
python scripts/generate_repo.py --no-sign
```

## Repository Structure

Generated repository layout:

```
rpm-repo/
├── packages/
│   ├── *.rpm                    (downloaded packages)
│   └── repodata/
│       ├── repomd.xml           (repository metadata)
│       ├── repomd.xml.asc       (GPG signature, if signed)
│       ├── primary.xml.gz       (package metadata)
│       ├── filelists.xml.gz     (file listings)
│       └── other.xml.gz         (changelogs)
├── packages.json                (manifest for incremental updates)
├── bordeux.repo                 (ready-to-use .repo file)
└── RPM-GPG-KEY-bordeux          (GPG public key, if signed)
```

## Requirements

- Python 3.9+
- PyYAML
- `createrepo_c` or `createrepo` (for generating repository metadata)
- `rpm` (optional, for extracting package metadata)
- GPG (optional, for signing)

### Installing createrepo_c

**Fedora/RHEL/CentOS:**
```bash
sudo dnf install createrepo_c
```

**Ubuntu/Debian:**
```bash
sudo apt-get install createrepo-c
```

**macOS:**
```bash
brew install createrepo_c
```

## License

MIT
