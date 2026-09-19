# Neo4j graph view

Set `GRAPH_ENABLED=true` with the `NEO4J_URI`, `NEO4J_USERNAME`,
`NEO4J_PASSWORD`, and optional `NEO4J_DATABASE` values from `.env.example`.
The pipeline writes idempotently with `MERGE`; running the same cached slice
twice does not duplicate graph nodes or relationships. With
`GRAPH_ENABLED=false`, the graph adapter emits a skip event and the rest of
the pipeline continues using its SQLite path.

The following queries can be pasted into Neo4j Explore or Bloom. Replace the
parameters in the first, fourth, and fifth queries with values from the run.

## 1. Returning participants and outcomes

```cypher
MATCH (p:Person)-[r:PARTICIPATED]->(e:Event {name: $event})
RETURN p.id AS person_id,
       p.name AS name,
       r.checked_in AS checked_in,
       r.placed AS placed
ORDER BY placed IS NULL, placed, name
```

## 2. Fork-heavy signals by evidence level

The profile stage stores its deterministic evidence bucket on `Person`.
`thin` is the useful graph-level view of fork-heavy or otherwise weak
original-work evidence; inspect the source profile before making a claim.

```cypher
MATCH (p:Person)
WHERE p.evidence_level IN ['thin', 'none']
RETURN p.id AS person_id,
       p.name AS name,
       p.evidence_level AS evidence_level,
       p.original_work_score AS original_work_score
ORDER BY evidence_level, original_work_score, name
```

## 3. Cohort skill histogram

```cypher
MATCH (p:Person)-[r:HAS_SKILL]->(s:Skill)
RETURN s.name AS skill,
       count(DISTINCT p) AS people,
       round(avg(r.confidence) * 100) / 100.0 AS average_confidence
ORDER BY people DESC, skill
```

## 4. A team and its skill coverage

```cypher
MATCH (p:Person)-[:MEMBER_OF]->(t:Team {id: $team_id})
OPTIONAL MATCH (p)-[r:HAS_SKILL]->(s:Skill)
RETURN t.id AS team_id,
       t.event AS event,
       collect(DISTINCT p.name) AS members,
       s.name AS skill,
       max(r.confidence) AS confidence
ORDER BY skill
```

## 5. One person's full neighbourhood

```cypher
MATCH (p:Person {id: $person_id})
OPTIONAL MATCH path=(p)-[*1..2]-(neighbour)
RETURN p, path, neighbour
LIMIT 100
```

We do not use Neo4j Graph Data Science (GDS). Aura Free is enough for the
identity, history, alias, teammate, and skill-coverage traversals used by the
demo, and omitting GDS keeps the sponsor lane optional.
