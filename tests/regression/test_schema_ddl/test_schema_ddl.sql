CREATE TABLE dataset (
	id INTEGER NOT NULL,
	id_project_specific VARCHAR NOT NULL,
	project VARCHAR NOT NULL,
	model VARCHAR NOT NULL,
	institution VARCHAR NOT NULL,
	experiment VARCHAR NOT NULL,
	variant_label VARCHAR NOT NULL,
	variable VARCHAR NOT NULL,
	reporting_interval VARCHAR NOT NULL,
	grid_label VARCHAR,
	processing_id VARCHAR NOT NULL,
	CONSTRAINT pk_dataset PRIMARY KEY (id)
);

CREATE UNIQUE INDEX dataset_uniqueness_idx ON dataset (id_project_specific, project, model, institution, experiment, variant_label, variable, reporting_interval, coalesce(grid_label, ''), processing_id);

CREATE INDEX ix_dataset_id_project_specific ON dataset (id_project_specific);

CREATE TABLE datasetnodeinformation (
	id INTEGER NOT NULL,
	data_node VARCHAR NOT NULL,
	CONSTRAINT pk_datasetnodeinformation PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_datasetnodeinformation_data_node ON datasetnodeinformation (data_node);

CREATE TABLE datasetrawdoc (
	id INTEGER NOT NULL,
	esgf_doc_id VARCHAR NOT NULL,
	raw_json VARCHAR NOT NULL,
	search_api_tag VARCHAR NOT NULL,
	retrieved_at DATETIME NOT NULL,
	CONSTRAINT pk_datasetrawdoc PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_datasetrawdoc_esgf_doc_id ON datasetrawdoc (esgf_doc_id);

CREATE TABLE searchapicallrecord (
	id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	host VARCHAR NOT NULL,
	http_method VARCHAR NOT NULL,
	url VARCHAR NOT NULL,
	request_body VARCHAR,
	response_code INTEGER,
	success BOOLEAN NOT NULL,
	error VARCHAR,
	num_results INTEGER,
	response_time_seconds FLOAT NOT NULL,
	attempt_number INTEGER NOT NULL,
	CONSTRAINT pk_searchapicallrecord PRIMARY KEY (id)
);

CREATE INDEX ix_searchapicallrecord_created_at ON searchapicallrecord (created_at);

CREATE INDEX ix_searchapicallrecord_host ON searchapicallrecord (host);

CREATE TABLE datasetversion (
	id INTEGER NOT NULL,
	dataset_id INTEGER NOT NULL,
	version VARCHAR NOT NULL,
	is_latest BOOLEAN NOT NULL,
	retracted BOOLEAN NOT NULL,
	CONSTRAINT pk_datasetversion PRIMARY KEY (id),
	CONSTRAINT uq_datasetversion_dataset_id_version UNIQUE (dataset_id, version),
	CONSTRAINT fk_datasetversion_dataset_id_dataset FOREIGN KEY(dataset_id) REFERENCES dataset (id)
);

CREATE INDEX ix_datasetversion_dataset_id ON datasetversion (dataset_id);

CREATE TABLE datasetversionnodelink (
	id INTEGER NOT NULL,
	dataset_version_id INTEGER NOT NULL,
	node_id INTEGER NOT NULL,
	CONSTRAINT pk_datasetversionnodelink PRIMARY KEY (id),
	CONSTRAINT uq_datasetversionnodelink_dataset_version_id_node_id UNIQUE (dataset_version_id, node_id),
	CONSTRAINT fk_datasetversionnodelink_dataset_version_id_datasetversion FOREIGN KEY(dataset_version_id) REFERENCES datasetversion (id),
	CONSTRAINT fk_datasetversionnodelink_node_id_datasetnodeinformation FOREIGN KEY(node_id) REFERENCES datasetnodeinformation (id)
);

CREATE INDEX ix_datasetversionnodelink_dataset_version_id ON datasetversionnodelink (dataset_version_id);

CREATE INDEX ix_datasetversionnodelink_node_id ON datasetversionnodelink (node_id);

CREATE TABLE rawdocversionlink (
	id INTEGER NOT NULL,
	raw_id INTEGER NOT NULL,
	dataset_version_id INTEGER NOT NULL,
	CONSTRAINT pk_rawdocversionlink PRIMARY KEY (id),
	CONSTRAINT uq_rawdocversionlink_raw_id_dataset_version_id UNIQUE (raw_id, dataset_version_id),
	CONSTRAINT fk_rawdocversionlink_raw_id_datasetrawdoc FOREIGN KEY(raw_id) REFERENCES datasetrawdoc (id),
	CONSTRAINT fk_rawdocversionlink_dataset_version_id_datasetversion FOREIGN KEY(dataset_version_id) REFERENCES datasetversion (id)
);

CREATE INDEX ix_rawdocversionlink_dataset_version_id ON rawdocversionlink (dataset_version_id);

CREATE INDEX ix_rawdocversionlink_raw_id ON rawdocversionlink (raw_id);
