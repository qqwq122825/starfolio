import { sqliteTable, text, integer } from 'drizzle-orm/sqlite-core';
export const workspaces = sqliteTable('workspaces', {
 owner: text('owner').primaryKey(), revision: integer('revision').notNull().default(0),
 payload: text('payload').notNull(), updatedAt: text('updated_at').notNull()
});
export const operations = sqliteTable('operations', {
 key: text('key').primaryKey(), owner: text('owner').notNull(),
 requestHash: text('request_hash').notNull(), response: text('response').notNull(), createdAt: text('created_at').notNull()
});
