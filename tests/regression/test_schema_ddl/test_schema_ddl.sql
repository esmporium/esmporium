CREATE TABLE datanode (
	id INTEGER NOT NULL,
	data_node VARCHAR NOT NULL,
	CONSTRAINT pk_datanode PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_datanode_data_node ON datanode (data_node);

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

CREATE TABLE datasetrawdoc (
	id INTEGER NOT NULL,
	esgf_doc_id VARCHAR NOT NULL,
	raw_json VARCHAR NOT NULL,
	raw_docs_format_tag VARCHAR NOT NULL,
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

CREATE TABLE datasetversiondatanodelink (
	id INTEGER NOT NULL,
	dataset_version_id INTEGER NOT NULL,
	data_node_id INTEGER NOT NULL,
	CONSTRAINT pk_datasetversiondatanodelink PRIMARY KEY (id),
	CONSTRAINT uq_datasetversiondatanodelink_dataset_version_id_data_node_id UNIQUE (dataset_version_id, data_node_id),
	CONSTRAINT fk_datasetversiondatanodelink_dataset_version_id_datasetversion FOREIGN KEY(dataset_version_id) REFERENCES datasetversion (id),
	CONSTRAINT fk_datasetversiondatanodelink_data_node_id_datanode FOREIGN KEY(data_node_id) REFERENCES datanode (id)
);

CREATE INDEX ix_datasetversiondatanodelink_data_node_id ON datasetversiondatanodelink (data_node_id);

CREATE INDEX ix_datasetversiondatanodelink_dataset_version_id ON datasetversiondatanodelink (dataset_version_id);

CREATE TABLE rawdocversionlink (
	id INTEGER NOT NULL,
	raw_doc_id INTEGER NOT NULL,
	dataset_version_id INTEGER NOT NULL,
	CONSTRAINT pk_rawdocversionlink PRIMARY KEY (id),
	CONSTRAINT uq_rawdocversionlink_raw_doc_id_dataset_version_id UNIQUE (raw_doc_id, dataset_version_id),
	CONSTRAINT fk_rawdocversionlink_raw_doc_id_datasetrawdoc FOREIGN KEY(raw_doc_id) REFERENCES datasetrawdoc (id),
	CONSTRAINT fk_rawdocversionlink_dataset_version_id_datasetversion FOREIGN KEY(dataset_version_id) REFERENCES datasetversion (id)
);

CREATE INDEX ix_rawdocversionlink_dataset_version_id ON rawdocversionlink (dataset_version_id);

CREATE INDEX ix_rawdocversionlink_raw_doc_id ON rawdocversionlink (raw_doc_id);
