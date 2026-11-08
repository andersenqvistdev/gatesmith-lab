# /company-new-project — Create New Project Under Company

Create a new project from a template and auto-register it with the company. This creates a new project FROM SCRATCH with scaffolding.

**Difference from /company-add-project:** This command creates a NEW project from a template. Use `/company-add-project` to register an EXISTING project.

## Input
$ARGUMENTS

Usage:
- `/company-new-project "Project Name"` — Create with default template (minimal)
- `/company-new-project "My API" --template python-lib` — Create with specific template
- `/company-new-project "API Service" --path ./services/api` — Create at specific path
- `/company-new-project "Frontend" --template node-frontend --tech-stack react,typescript` — With additional tech
- `/company-new-project --list-templates` — Show available templates

Options:
- `--template TEMPLATE` — Template to use (default: minimal)
- `--path PATH` — Project path (default: ./projects/{project-name-slug})
- `--tech-stack LIST` — Comma-separated additional technologies
- `--list-templates` — Show available templates and exit
- `--preview` — Show what would be created without making changes

## Step 0: Verify Company Root Exists

Search upward from current directory for `.forge-company-root`:

```bash
# Using company_resolver.py to find company root
uv run .claude/hooks/company/company_resolver.py find
```

**If no company root found:**
```
## No Company Root Found

Cannot create project — no multi-project company root exists.

To create a company root first, run from the parent directory:
  /company-create

Or create a single-project company in this project:
  /company-init
```

Exit without changes.

## Step 1: Handle --list-templates

If `--list-templates` flag is present in arguments:

Scan the templates directory and read each `_template.json`:

```bash
# List template directories
ls -1 .company/templates/projects/
```

For each template, read its `_template.json` to get metadata.

**Display:**
```
## Available Project Templates

| Template | Description | Tech Stack |
|----------|-------------|------------|
| minimal | Minimal project scaffold with just planning and claude directories | - |
| python-lib | Python library project with pytest and modern pyproject.toml configuration | python, pytest, uv |
| python-cli | Python CLI application with click and entry point configuration | python, click, pytest, uv |
| node-api | Node.js REST API with Express and modern ES module configuration | node, javascript, express, jest |
| node-frontend | Node.js frontend with React and modern build tooling | node, react, javascript, vite |

### Usage Examples

```bash
# Create a Python library
/company-new-project "My Library" --template python-lib

# Create a Node API service
/company-new-project "API Gateway" --template node-api --path ./services/gateway

# Create minimal project for prototyping
/company-new-project "Experiment" --template minimal
```
```

Exit after displaying templates.

## Step 2: Parse Arguments

Parse `$ARGUMENTS`:
- Extract project name (first positional argument, required unless --list-templates)
- Extract `--template` value (default: "minimal")
- Extract `--path` value (default: derive from project name)
- Extract `--tech-stack` value (comma-separated list)
- Check for `--preview` flag

**If no project name provided (and not --list-templates):**
```
## Missing Project Name

Usage: /company-new-project "Project Name" [--template TEMPLATE] [--path PATH]

Examples:
  /company-new-project "My API"
  /company-new-project "Frontend App" --template node-frontend
  /company-new-project --list-templates

Provide a project name as the first argument.
```

Exit without changes.

## Step 3: Validate Template Exists

Check that the specified template exists:

```bash
ls -la .company/templates/projects/[template]/
```

**If template not found:**
```
## Template Not Found

The template '[template]' does not exist.

Available templates:
- minimal
- python-lib
- python-cli
- node-api
- node-frontend

Run `/company-new-project --list-templates` for details.
```

Exit without changes.

## Step 4: Resolve Project Path

Determine the project path:
1. If `--path` provided, use that path
2. Otherwise, generate from project name: `./projects/{slugified-name}`

Slugify the project name:
- Convert to lowercase
- Replace spaces and special characters with hyphens
- Remove leading/trailing hyphens

Validate the path:
- Must be within or relative to the company root
- Must not already exist (unless empty directory)

```bash
# Check if path already exists
ls -la [resolved_path] 2>/dev/null
```

**If path exists and is not empty:**
```
## Path Already Exists

The specified path already exists and is not empty.

**Path:** [resolved_path]

Options:
1. Choose a different path: `/company-new-project "Name" --path ./different/path`
2. Register existing project: `/company-add-project [path]`
3. Remove the existing directory and retry
```

Exit without changes.

## Step 5: Show Preview (if --preview flag)

If `--preview` flag is set, show what would be created:

```
## Preview: New Project Creation

**Project Name:** [name]
**Project ID:** [generated-id]
**Path:** [resolved_path]
**Template:** [template]
**Tech Stack:** [template tech + additional tech]

### Files to Create

From template '[template]':
- .claude/
- .planning/PROJECT.md
- .planning/REQUIREMENTS.md
- .planning/ROADMAP.md
- [other template-specific files]

### Registration

Will be registered in:
- .company/org.json (projects array)

### Next Steps After Creation

1. Initialize git: `cd [path] && git init`
2. Review generated files
3. Start development: `/plan`

---

Run without `--preview` to create the project.
```

Exit without changes.

## Step 6: Create Project Using project_orchestrator

Call the project creation function:

```bash
uv run .claude/hooks/company/project_orchestrator.py create \
    --name "[Project Name]" \
    --path "[resolved_path]" \
    --template "[template]" \
    --tech-stack "[tech1,tech2,...]"
```

Parse the JSON response:
- `success`: Whether creation succeeded
- `project_id`: Generated project ID
- `project_path`: Absolute path to created project
- `scaffolded`: Whether template was applied
- `registered`: Whether registered with company
- `errors`: List of any errors

**If creation failed:**
```
## Project Creation Failed

Failed to create project '[name]'.

**Errors:**
[list of errors from response]

**Possible causes:**
- Insufficient permissions
- Disk space issues
- Invalid template

Check the error messages above and retry.
```

Exit without changes.

## Step 7: Display Success Summary

```
## Project Created Successfully

=====================================================================
 NEW PROJECT                                                 [success]
=====================================================================
 Project: [Project Name]
 ID: [project_id]
 Path: [relative_path]
 Template: [template]
=====================================================================

### Project Details
| Field | Value |
|-------|-------|
| Project ID | [project_id] |
| Display Name | [Project Name] |
| Path | [project_path] |
| Template | [template] |
| Tech Stack | [comma-separated list] |
| Created | [timestamp] |

### Files Created
| File/Directory | Description |
|----------------|-------------|
| .claude/ | Claude Code configuration directory |
| .planning/PROJECT.md | Project context and conventions |
| .planning/REQUIREMENTS.md | Requirements document |
| .planning/ROADMAP.md | Development roadmap |
| CLAUDE.md | Project instructions |
| [template-specific files] | [descriptions] |

### Registration
| File | Status |
|------|--------|
| .company/org.json | Updated - project added |

=====================================================================

### Next Steps

1. **Navigate to project:**
   ```bash
   cd [project_path]
   ```

2. **Initialize version control (if needed):**
   ```bash
   git init
   ```

3. **Start planning:**
   ```bash
   /discuss  # Capture requirements
   /plan     # Create development plan
   ```

4. **Assign employees:**
   ```bash
   /company-assign [employee-id] [project_id]
   ```

### Related Commands
- `/company-projects` — List all registered projects
- `/company-status` — View company and project status
- `/company-assign` — Assign employees to this project
- `/discuss` — Start requirements discussion
```

## Rules

1. **Always verify company root first.** Projects must be created within a company context.

2. **Validate template existence.** Never attempt to use a non-existent template.

3. **Never overwrite existing projects.** If the path exists and has content, refuse and suggest alternatives.

4. **Use project_orchestrator for all operations.** The orchestrator handles scaffolding, registration, and validation.

5. **Provide clear feedback.** Show what was created and what to do next.

6. **Default to minimal template.** If no template specified, use the minimal template for flexibility.

7. **Auto-generate sensible paths.** If no path provided, create under `./projects/` with slugified name.

8. **Merge tech stacks.** Combine template tech stack with any additional `--tech-stack` values.

## Error Handling

### Permission Denied

```
## Permission Denied

Cannot write to the specified path.

**Path:** [path]
**Error:** [error details]

Check that you have write permissions to this directory.
```

### Template Copy Failed

```
## Template Copy Failed

Failed to copy template files.

**Template:** [template]
**Error:** [error details]

The template may be corrupted or missing files. Try a different template.
```

### Registration Failed

```
## Registration Failed

Project was created but could not be registered with the company.

**Path:** [project_path]
**Error:** [error details]

The project files exist at the path above. You can manually register it:
  /company-add-project [project_path]
```

### Invalid Path (Outside Company)

```
## Invalid Path

The specified path is outside the company root.

**Company Root:** [company_root]
**Requested Path:** [path]

Projects must be created within the company directory tree. Suggested paths:
- ./projects/[name]
- ./services/[name]
- ./packages/[name]
```
