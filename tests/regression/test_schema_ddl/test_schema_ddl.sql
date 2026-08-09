CREATE TABLE dataset (
	id VARCHAR NOT NULL,
	project VARCHAR NOT NULL,
	model VARCHAR NOT NULL,
	institution VARCHAR NOT NULL,
	experiment VARCHAR NOT NULL,
	variant_label VARCHAR NOT NULL,
	variable VARCHAR NOT NULL,
	reporting_interval VARCHAR NOT NULL,
	grid_label VARCHAR NOT NULL,
	processing_id VARCHAR NOT NULL,
	CONSTRAINT pk_dataset PRIMARY KEY (id)
);
