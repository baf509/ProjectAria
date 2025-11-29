# ARIA Changelog

All notable changes to ARIA will be documented in this file.

Format: 
```
## [Date] - Phase X - [Summary]

### Added
- New features

### Changed  
- Changes to existing features

### Fixed
- Bug fixes

### Removed
- Removed features

### Notes
- Important notes for future work
```

---

## [Unreleased]

### Added
- Initial project specification (`SPECIFICATION.md`)
- Project status tracking (`PROJECT_STATUS.md`)
- This changelog

### Changed
- Updated MongoDB configuration to use Community Server 8.2 + mongot
- Switched from `mongodb-atlas-local` to separate `mongod` + `mongot` services
- Updated embedding dimension to 4096 (Qwen3-8b)
- Added hybrid search (BM25 + Vector) with RRF fusion for long-term memory

### Notes
- Phase 1 work to begin
- See `PROJECT_STATUS.md` for current checklist
- MongoDB 8.2 Vector Search is in Public Preview
- mongot image: `mongodb/mongodb-community-search:0.53.1`
- mongod image: `mongodb/mongodb-community-server:8.2.0-ubi9`

---

<!-- 
Template for new entries:

## [YYYY-MM-DD] - Phase X - [Summary]

### Added
- 

### Changed
- 

### Fixed
- 

### Notes
- 

-->
