# /add-agent — Create a New Specialized Agent

> **DEPRECATED:** This command is deprecated in favor of `/agent`.
> `/agent` provides the same functionality plus:
> - Automatic consultant registration (when company context exists)
> - Memory persistence across sessions
> - Skill-based reuse of existing consultants
>
> This command will continue to work but may be removed in a future version.

Use the Meta-Agent to generate a new agent definition from a description.

## Input
$ARGUMENTS

## Process

1. Spawn the Meta-Agent:

```
Task(subagent_type="general-purpose", description="Generate new agent")
```

Pass the Meta-Agent:
- The user's description of what agent they need
- Instruction to read `.claude/agents/meta-agent.md` for full rules
- Instruction to read ALL existing agents in `.claude/agents/` first to maintain consistency

2. Verify the generated agent file was written to `.claude/agents/`

3. Present a summary:
```
Agent created: [name]
File: .claude/agents/[name].md
Role: [one-line description]
Tools: [list]

To use it, I'll reference this agent when spawning Task sub-agents
for relevant work. You can also ask me to use it directly:
  "Use the [name] agent to [do something]"
```

## Examples

```
> /add-agent A database migration specialist that generates SQL migrations,
  validates them against the current schema, and tests rollbacks

> /add-agent A documentation writer that reads source code and generates
  API documentation following JSDoc/docstring conventions

> /add-agent A performance profiler that identifies N+1 queries, unnecessary
  re-renders, and unbounded operations
```

## Rules
- The Meta-Agent reads existing agents to match the established pattern
- Each agent gets single responsibility, least privilege, structured output
- The file is written to `.claude/agents/` and is immediately usable
