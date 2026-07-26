import { v } from "convex/values";

import { internalMutation, query } from "./_generated/server";

const DEMO_DEPLOYMENT = "demo-local";

const deploymentProjection = v.object({
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
});

const repositoryProjection = v.object({
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
});

const reviewProjection = v.object({
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
});

const qualityProjection = v.object({
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
});

const modelProjection = v.object({
  provider: v.string(),
  model: v.string(),
  status: v.union(
    v.literal("ready"),
    v.literal("missing_key"),
    v.literal("error"),
  ),
  reviewPasses: v.array(v.string()),
  updatedAt: v.number(),
});

export const dashboard = query({
  args: {},
  returns: v.object({
    deployment: v.union(
      v.null(),
      deploymentProjection.extend({
        _id: v.id("deployments"),
        _creationTime: v.number(),
      }),
    ),
    repositories: v.array(
      repositoryProjection.extend({
        _id: v.id("repositories"),
        _creationTime: v.number(),
        deploymentKey: v.string(),
      }),
    ),
    reviews: v.array(
      reviewProjection.extend({
        _id: v.id("reviewRuns"),
        _creationTime: v.number(),
        deploymentKey: v.string(),
      }),
    ),
    quality: v.union(
      v.null(),
      qualityProjection.extend({
        _id: v.id("qualitySnapshots"),
        _creationTime: v.number(),
        deploymentKey: v.string(),
      }),
    ),
    model: v.union(
      v.null(),
      modelProjection.extend({
        _id: v.id("modelProfiles"),
        _creationTime: v.number(),
        deploymentKey: v.string(),
      }),
    ),
  }),
  handler: async (ctx) => {
    // Only the synthetic preview is intentionally public. Real deployment
    // selection stays unavailable until tenant auth/authorization is wired.
    const deploymentKey = DEMO_DEPLOYMENT;
    const [deployment, repositories, reviews, quality, model] =
      await Promise.all([
        ctx.db
          .query("deployments")
          .withIndex("by_deployment_key", (q) =>
            q.eq("deploymentKey", deploymentKey),
          )
          .unique(),
        ctx.db
          .query("repositories")
          .withIndex("by_deployment_key_and_updated_at", (q) =>
            q.eq("deploymentKey", deploymentKey),
          )
          .order("desc")
          .take(50),
        ctx.db
          .query("reviewRuns")
          .withIndex("by_deployment_key_and_updated_at", (q) =>
            q.eq("deploymentKey", deploymentKey),
          )
          .order("desc")
          .take(40),
        ctx.db
          .query("qualitySnapshots")
          .withIndex("by_deployment_key_and_evaluated_at", (q) =>
            q.eq("deploymentKey", deploymentKey),
          )
          .order("desc")
          .first(),
        ctx.db
          .query("modelProfiles")
          .withIndex("by_deployment_key", (q) =>
            q.eq("deploymentKey", deploymentKey),
          )
          .unique(),
      ]);
    return { deployment, repositories, reviews, quality, model };
  },
});

// Non-sensitive sample projections for the development workspace. Production
// projections never contain source, diffs, prompts, or finding bodies.
export const bootstrapDemo = internalMutation({
  args: {},
  returns: v.object({ created: v.boolean() }),
  handler: async (ctx) => {
    const existing = await ctx.db
      .query("deployments")
      .withIndex("by_deployment_key", (q) =>
        q.eq("deploymentKey", DEMO_DEPLOYMENT),
      )
      .unique();
    if (existing) return { created: false };

    const now = Date.now();
    await ctx.db.insert("deployments", {
      deploymentKey: DEMO_DEPLOYMENT,
      name: "Diffuse Osaka",
      kind: "self_hosted",
      region: "Local Docker",
      status: "healthy",
      version: "0.1.0-dev",
      updatedAt: now,
    });

    const repositories = [
      {
        externalId: "diffuse",
        name: "diffuse/diffuse",
        provider: "github" as const,
        defaultBranch: "main",
        indexStatus: "ready" as const,
        reviewStatus: "idle" as const,
        lastIndexedAt: now - 4 * 60_000,
        lastReviewAt: now - 18 * 60_000,
        openFindingCount: 3,
        criticalFindingCount: 0,
      },
      {
        externalId: "api",
        name: "diffuse/api",
        provider: "github" as const,
        defaultBranch: "main",
        indexStatus: "indexing" as const,
        reviewStatus: "reviewing" as const,
        lastIndexedAt: now - 2 * 60 * 60_000,
        lastReviewAt: now - 8 * 60_000,
        openFindingCount: 1,
        criticalFindingCount: 1,
      },
      {
        externalId: "docs",
        name: "diffuse/docs",
        provider: "gitlab" as const,
        defaultBranch: "main",
        indexStatus: "stale" as const,
        reviewStatus: "attention" as const,
        lastIndexedAt: now - 26 * 60 * 60_000,
        lastReviewAt: now - 8 * 60 * 60_000,
        openFindingCount: 0,
        criticalFindingCount: 0,
      },
    ];
    for (const repository of repositories) {
      await ctx.db.insert("repositories", {
        deploymentKey: DEMO_DEPLOYMENT,
        ...repository,
        updatedAt: now,
      });
    }

    const reviews = [
      {
        externalId: "review-184",
        repositoryExternalId: "api",
        repositoryName: "diffuse/api",
        number: 184,
        title: "Harden webhook signature validation",
        status: "reviewing" as const,
        findingCount: 1,
        criticalCount: 1,
        model: "openai/gpt-4.1-mini",
        updatedAt: now - 36_000,
      },
      {
        externalId: "review-182",
        repositoryExternalId: "diffuse",
        repositoryName: "diffuse/diffuse",
        number: 182,
        title: "Add self-host runtime preflight",
        status: "published" as const,
        findingCount: 3,
        criticalCount: 0,
        latencyMs: 44_200,
        model: "openai/gpt-4.1-mini",
        updatedAt: now - 18 * 60_000,
      },
      {
        externalId: "review-177",
        repositoryExternalId: "docs",
        repositoryName: "diffuse/docs",
        number: 177,
        title: "Document GitLab installation flow",
        status: "failed" as const,
        findingCount: 0,
        criticalCount: 0,
        latencyMs: 9_100,
        model: "openai/gpt-4.1-mini",
        updatedAt: now - 8 * 60 * 60_000,
      },
    ];
    for (const review of reviews) {
      await ctx.db.insert("reviewRuns", {
        deploymentKey: DEMO_DEPLOYMENT,
        ...review,
      });
    }

    await ctx.db.insert("qualitySnapshots", {
      deploymentKey: DEMO_DEPLOYMENT,
      evaluatedAt: now,
      precision: 0.86,
      recall: 0.74,
      f1: 0.795,
      truePositives: 37,
      falsePositives: 6,
      falseNegatives: 13,
      addressedFindings: 21,
      sampleSize: 56,
      medianLatencyMs: 44_200,
      estimatedCostUsd: 0.12,
    });
    await ctx.db.insert("modelProfiles", {
      deploymentKey: DEMO_DEPLOYMENT,
      provider: "OpenAI",
      model: "openai/gpt-4.1-mini",
      status: "missing_key",
      reviewPasses: ["correctness", "security", "performance", "tests"],
      updatedAt: now,
    });
    return { created: true };
  },
});

export const ingestSnapshot = internalMutation({
  args: {
    deployment: deploymentProjection,
    repositories: v.array(repositoryProjection),
    reviews: v.array(reviewProjection),
    quality: v.optional(qualityProjection),
    model: v.optional(modelProjection),
  },
  returns: v.object({ accepted: v.boolean() }),
  handler: async (ctx, args) => {
    const deploymentKey = args.deployment.deploymentKey;
    const existingDeployment = await ctx.db
      .query("deployments")
      .withIndex("by_deployment_key", (q) =>
        q.eq("deploymentKey", deploymentKey),
      )
      .unique();
    if (existingDeployment) {
      await ctx.db.patch(existingDeployment._id, args.deployment);
    } else {
      await ctx.db.insert("deployments", args.deployment);
    }

    for (const repository of args.repositories) {
      const existing = await ctx.db
        .query("repositories")
        .withIndex("by_deployment_key_and_external_id", (q) =>
          q
            .eq("deploymentKey", deploymentKey)
            .eq("externalId", repository.externalId),
        )
        .unique();
      const value = { deploymentKey, ...repository };
      if (existing) await ctx.db.patch(existing._id, value);
      else await ctx.db.insert("repositories", value);
    }

    for (const review of args.reviews) {
      const existing = await ctx.db
        .query("reviewRuns")
        .withIndex("by_deployment_key_and_external_id", (q) =>
          q
            .eq("deploymentKey", deploymentKey)
            .eq("externalId", review.externalId),
        )
        .unique();
      const value = { deploymentKey, ...review };
      if (existing) await ctx.db.patch(existing._id, value);
      else await ctx.db.insert("reviewRuns", value);
    }

    if (args.quality) {
      const existing = await ctx.db
        .query("qualitySnapshots")
        .withIndex("by_deployment_key_and_evaluated_at", (q) =>
          q
            .eq("deploymentKey", deploymentKey)
            .eq("evaluatedAt", args.quality!.evaluatedAt),
        )
        .unique();
      const value = { deploymentKey, ...args.quality };
      if (existing) await ctx.db.patch(existing._id, value);
      else await ctx.db.insert("qualitySnapshots", value);
    }
    if (args.model) {
      const existing = await ctx.db
        .query("modelProfiles")
        .withIndex("by_deployment_key", (q) =>
          q.eq("deploymentKey", deploymentKey),
        )
        .unique();
      const value = { deploymentKey, ...args.model };
      if (existing) await ctx.db.patch(existing._id, value);
      else await ctx.db.insert("modelProfiles", value);
    }
    return { accepted: true };
  },
});
