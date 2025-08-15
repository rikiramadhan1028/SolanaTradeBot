import { cfg } from './config.js';

type Doc = Record<string, any> & { _id?: string };

let useMongo = !!cfg.mongoUrl;
let mongoose: any;
let TradeModel: any;

export async function connectDb() {
  if (!useMongo) {
    console.log('[db] Mongo disabled (no MONGO_URL). Using in-memory logs.');
    return;
  }
  try {
    mongoose = (await import('mongoose')).default;
    await mongoose.connect(cfg.mongoUrl);
    const { Schema, model } = mongoose;

    const TradeSchema = new Schema({
      createdAt: { type: Date, default: Date.now },
      type: { type: String, enum: ['pumpfun', 'dex'], required: true },
      action: { type: String, enum: ['buy', 'sell', 'swap'], required: true },
      dex: { type: String },
      mintIn: { type: String },
      mintOut: { type: String },
      amount: { type: Schema.Types.Mixed },
      jito: { type: Boolean, default: false },
      signature: { type: String },
      status: { type: String, enum: ['submitted', 'confirmed', 'failed'], default: 'submitted' },
      error: { type: Schema.Types.Mixed },
      request: { type: Schema.Types.Mixed },
      mint: { type: String } // for pumpfun
    });
    TradeModel = model('Trade', TradeSchema);
    console.log('[db] Connected to Mongo.');
  } catch (e) {
    console.log('[db] Mongo connection failed, falling back to in-memory:', e);
    useMongo = false;
  }
}

// Common interface used elsewhere
export const Trade = useMongo
  ? {
      async create(doc: Doc) {
        return TradeModel.create(doc);
      },
      async findByIdAndUpdate(id: string, update: Doc) {
        return TradeModel.findByIdAndUpdate(id, update);
      },
    }
  : (function () {
      // simple in-memory store
      const mem = new Map<string, Doc>();
      const newId = () => Math.random().toString(36).slice(2) + Date.now().toString(36);
      return {
        async create(doc: Doc) {
          const _id = newId();
          const rec = { ...doc, _id };
          mem.set(_id, rec);
          return rec;
        },
        async findByIdAndUpdate(id: string, update: Doc) {
          const curr = mem.get(id);
          if (!curr) return null;
          Object.assign(curr, update);
          mem.set(id, curr);
          return curr;
        },
      };
    })();
