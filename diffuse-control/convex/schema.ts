import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  deployments: defineTable({
    deploymentKey: v.string(),
    name: v.string(),
    kind: v.union(v.literal("self_hosted"), v.literal("managed")),
    region: v.optional(v.string()),
    status: v.union(
      v.literal("healthy"),
      v.literal("degraded"),
      v.literal("offline"),
    ),
    version: v.string(),
    updatedAt: v.number(),
  }).index("by_deployment_key", ["deploymentKey"]),

  repositories: defineTable({
    deploymentKey: v.string(),
    externalId: v.string(),
    name: v.string(),
    provider: v.union(v.literal("github"), v.literal("gitlab")),
    defaultBranch: v.string(),
    indexStatus: v.union(
      v.literal("ready"),
      v.literal("indexing"),
      v.literal("stale"),
      v.literal("failed"),
    ),
    reviewStatus: v.union(
      v.literal("idle"),
      v.literal("reviewing"),
      v.literal("attention"),
    ),
    lastIndexedAt: v.optional(v.number()),
    lastReviewAt: v.optional(v.number()),
    openFindingCount: v.number(),
    criticalFindingCount: v.number(),
    updatedAt: v.number(),
  })
    .index("by_deployment_key_and_external_id", [
      "deploymentKey",
      "externalId",
    ])
    .index("by_deployment_key_and_updated_at", [
      "deploymentKey",
      "updatedAt",
    ]),

  reviewRuns: defineTable({
    deploymentKey: v.string(),
    externalId: v.string(),
    repositoryExternalId: v.string(),
    repositoryName: v.string(),
    number: v.optional(v.number()),
    title: v.string(),
    status: v.union(
      v.literal("queued"),
      v.literal("reviewing"),
      v.literal("published"),
      v.literal("failed"),
    ),
    findingCount: v.number(),
    criticalCount: v.number(),
    latencyMs: v.optional(v.number()),
    model: v.string(),
    updatedAt: v.number(),
  })
    .index("by_deployment_key_and_external_id", [
      "deploymentKey",
      "externalId",
    ])
    .index("by_deployment_key_and_updated_at", [
      "deploymentKey",
      "updatedAt",
    ]),

  qualitySnapshots: defineTable({
    deploymentKey: v.string(),
    evaluatedAt: v.number(),
    precision: v.number(),
    recall: v.number(),
    f1: v.number(),
    truePositives: v.number(),
    falsePositives: v.number(),
    falseNegatives: v.number(),
    addressedFindings: v.number(),
    sampleSize: v.number(),
    medianLatencyMs: v.number(),
    estimatedCostUsd: v.number(),
  }).index("by_deployment_key_and_evaluated_at", [
    "deploymentKey",
    "evaluatedAt",
  ]),

  modelProfiles: defineTable({
    deploymentKey: v.string(),
    provider: v.string(),
    model: v.string(),
    status: v.union(
      v.literal("ready"),
      v.literal("missing_key"),
      v.literal("error"),
    ),
    reviewPasses: v.array(v.string()),
    updatedAt: v.number(),
  }).index("by_deployment_key", ["deploymentKey"]),
});
