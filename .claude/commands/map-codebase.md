# /map-codebase — Deep Codebase Mapping with Parallel Agents (from GSD)

Build a comprehensive map of the codebase using parallel exploration agents.

## Input
$ARGUMENTS

## Step 1: Parallel Exploration

Launch multiple Explorer agents simultaneously to map different aspects:

```
Task(subagent_type="Explore", description="Map project structure and architecture")
Task(subagent_type="Explore", description="Map API endpoints and routes")
Task(subagent_type="Explore", description="Map data models and schemas")
Task(subagent_type="Explore", description="Map test structure and coverage patterns")
```

Each agent explores a specific dimension of the codebase.

## Step 2: Synthesize

Combine all agent outputs into a unified map.

## Step 3: Update Project Intelligence

Write the combined map to `.planning/PROJECT.md`:
- Architecture overview
- Key directories and their purposes
- Tech stack details
- Conventions discovered
- Key files and entry points

## Step 4: Present

```
## Codebase Map

### Structure
[directory tree with annotations]

### Architecture
[high-level description]

### Key Files
| File | Purpose |
|------|---------|

### API Surface
[endpoints/routes if applicable]

### Data Models
[schemas/models if applicable]

### Test Patterns
[how tests are organized]

### Conventions
[coding patterns, naming, etc.]
```
