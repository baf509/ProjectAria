// scripts/init-mongo.js
// Run after replica set is initialized to create indexes

// Wait for replica set to be ready
let attempts = 0;
while (attempts < 30) {
  try {
    const status = rs.status();
    if (status.ok === 1) {
      print("Replica set is ready");
      break;
    }
  } catch (e) {
    print("Waiting for replica set... attempt " + attempts);
    sleep(2000);
  }
  attempts++;
}

// Switch to aria database
db = db.getSiblingDB('aria');

// =============================================================================
// Create Collections
// =============================================================================

// Create collections if they don't exist
const collections = ['conversations', 'memories', 'agents', 'tools', 'settings'];
const existingCollections = db.getCollectionNames();

collections.forEach(name => {
  if (!existingCollections.includes(name)) {
    db.createCollection(name);
    print("Created collection: " + name);
  }
});

// =============================================================================
// Standard Indexes
// =============================================================================

// Conversations indexes
db.conversations.createIndex({ status: 1, updated_at: -1 });
db.conversations.createIndex({ agent_id: 1 });
db.conversations.createIndex({ tags: 1 });
db.conversations.createIndex({ "messages.created_at": 1 });
print("Created conversation indexes");

// Memories indexes (standard)
db.memories.createIndex({ status: 1, importance: -1 });
db.memories.createIndex({ categories: 1 });
db.memories.createIndex({ content_type: 1 });
db.memories.createIndex({ "source.conversation_id": 1 });
db.memories.createIndex({ created_at: -1 });
print("Created memory indexes");

// Agents indexes
db.agents.createIndex({ slug: 1 }, { unique: true });
db.agents.createIndex({ is_default: 1 });
print("Created agent indexes");

// Tools indexes
db.tools.createIndex({ slug: 1 }, { unique: true });
db.tools.createIndex({ type: 1, status: 1 });
print("Created tool indexes");

// =============================================================================
// Search Indexes (Atlas Search / mongot)
// =============================================================================

// Note: Search indexes are created via createSearchIndex() method
// These require mongot to be running and connected

try {
  // Vector Search Index for memories
  // qwen3-embedding:0.6b produces 1024-dimensional embeddings
  db.memories.createSearchIndex({
    name: "memory_vector_index",
    type: "vectorSearch",
    definition: {
      fields: [
        {
          type: "vector",
          path: "embedding",
          numDimensions: 1024,
          similarity: "cosine"
        },
        {
          type: "filter",
          path: "status"
        },
        {
          type: "filter",
          path: "categories"
        },
        {
          type: "filter",
          path: "content_type"
        }
      ]
    }
  });
  print("Created vector search index: memory_vector_index");
} catch (e) {
  print("Vector search index may already exist or mongot not ready: " + e.message);
}

try {
  // Text Search Index for memories (BM25)
  db.memories.createSearchIndex({
    name: "memory_text_index",
    type: "search",
    definition: {
      mappings: {
        dynamic: false,
        fields: {
          content: {
            type: "string",
            analyzer: "lucene.english"
          },
          categories: {
            type: "string",
            analyzer: "lucene.keyword"
          },
          content_type: {
            type: "string",
            analyzer: "lucene.keyword"
          },
          status: {
            type: "string",
            analyzer: "lucene.keyword"
          }
        }
      }
    }
  });
  print("Created text search index: memory_text_index");
} catch (e) {
  print("Text search index may already exist or mongot not ready: " + e.message);
}

// =============================================================================
// Default Agent
// =============================================================================

const defaultAgent = db.agents.findOne({ is_default: true });
if (!defaultAgent) {
  db.agents.insertOne({
    name: "ARIA",
    slug: "aria",
    description: "Default AI assistant with memory and tool capabilities",
    system_prompt: `You are ARIA, a helpful AI assistant with long-term memory.

You remember facts and preferences from previous conversations and use them to provide personalized assistance.

When you learn something new about the user (preferences, facts, context), it will be stored in your memory for future conversations.

Be helpful, accurate, and personable. Use your memory to provide continuity across conversations.`,
    llm: {
      backend: "ollama",
      model: "qwen3:8b",
      temperature: 0.7,
      max_tokens: 4096
    },
    fallback_chain: [
      {
        backend: "anthropic",
        model: "claude-sonnet-4-5-20250929",
        conditions: {
          on_error: true,
          on_context_overflow: true,
          max_input_tokens: 100000
        }
      }
    ],
    capabilities: {
      memory_enabled: true,
      tools_enabled: true,
      computer_use_enabled: false
    },
    memory_config: {
      auto_extract: true,
      short_term_messages: 20,
      long_term_results: 10,
      categories_filter: null
    },
    enabled_tools: [],
    is_default: true,
    created_at: new Date(),
    updated_at: new Date()
  });
  print("Created default agent: ARIA");
}

// =============================================================================
// Default Settings
// =============================================================================

const settings = db.settings.findOne({ _id: "global" });
if (!settings) {
  db.settings.insertOne({
    _id: "global",
    llm: {
      ollama_url: "http://host.docker.internal:11434",
      anthropic_api_key: "",
      openai_api_key: "",
      default_backend: "ollama"
    },
    embedding: {
      provider: "ollama",
      ollama_model: "qwen3:8b",
      voyage_api_key: ""
    },
    memory: {
      auto_extract: true,
      extraction_model: "ollama/llama3.2:latest",
      importance_threshold: 0.5
    },
    ui: {
      theme: "dark",
      default_agent: "aria"
    },
    updated_at: new Date()
  });
  print("Created default settings");
}

print("\n=== MongoDB initialization complete ===");
