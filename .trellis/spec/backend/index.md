# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Database Guidelines](./database-guidelines.md) | ORM patterns, queries, migrations | To fill |
| [Error Handling](./error-handling.md) | Error types, handling strategies | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Code standards, forbidden patterns | To fill |
| [Account Inspection](./account-inspection.md) | Durable one-click inspection, failure-detail confidentiality, SQLite snapshots, and stale-list prevention | Ready |
| [Sub2API Credential Rotation](./sub2api-credential-rotation.md) | Verification-first Sub2API OpenAI OAuth retry, account-ID replacement proof, and cleanup gating | Ready |
| [ChatGPT2API Credential Rotation](./chatgpt2api-credential-rotation.md) | Verification-first expired AT detection, local liveness refresh, and exact remote-ID replacement | Ready |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
