# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-11-01

The initial public release of the Jira plugin.

### Added

- Jira Assets connections: connect a Jira Assets schema to an IndraMesh Source and keep the type mapping under your control.
- Bidirectional pull, push and export between Core objects and Jira Assets objects, with the sync direction chosen per level.
- Conflict handling, resumable checkpoints and an audit trail, so an interrupted run resumes where it stopped and every change stays traceable.
- Per-object badges and write protection for the objects the connector manages in Jira.

**Compatibility:** plugin version 1.0.0 requires IndraMesh Core 1.0.0.
It declares `api_version` 1.0.0.
