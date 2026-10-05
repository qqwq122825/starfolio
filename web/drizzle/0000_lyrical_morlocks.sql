CREATE TABLE `operations` (
	`key` text PRIMARY KEY NOT NULL,
	`owner` text NOT NULL,
	`request_hash` text NOT NULL,
	`response` text NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `workspaces` (
	`owner` text PRIMARY KEY NOT NULL,
	`revision` integer DEFAULT 0 NOT NULL,
	`payload` text NOT NULL,
	`updated_at` text NOT NULL
);
