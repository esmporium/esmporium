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

CREATE INDEX ix_dataset_id_project_specific ON dataset (id_project_specific);

CREATE UNIQUE INDEX uq_dataset_identity ON dataset (id_project_specific, project, model, institution, experiment, variant_label, variable, reporting_interval, coalesce(grid_label, ''), processing_id);

CREATE TABLE datasetrawdoc (
	id INTEGER NOT NULL,
	esgf_doc_id VARCHAR NOT NULL,
	search_host VARCHAR NOT NULL,
	raw_json VARCHAR NOT NULL,
	retrieved_at DATETIME NOT NULL,
	CONSTRAINT pk_datasetrawdoc PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_datasetrawdoc_esgf_doc_id ON datasetrawdoc (esgf_doc_id);

CREATE TABLE datasetversionspecific (
	version_id VARCHAR NOT NULL,
	id_project_specific VARCHAR NOT NULL,
	version VARCHAR NOT NULL,
	is_latest BOOLEAN NOT NULL,
	retracted BOOLEAN NOT NULL,
	CONSTRAINT pk_datasetversionspecific PRIMARY KEY (version_id)
);

CREATE INDEX ix_datasetversionspecific_id_project_specific ON datasetversionspecific (id_project_specific);

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

CREATE TABLE datasetnodeinformation (
	id INTEGER NOT NULL,
	version_id VARCHAR NOT NULL,
	data_node VARCHAR NOT NULL,
	index_node VARCHAR,
	replica BOOLEAN NOT NULL,
	CONSTRAINT pk_datasetnodeinformation PRIMARY KEY (id),
	CONSTRAINT uq_datasetnodeinformation_version_id_data_node UNIQUE (version_id, data_node),
	CONSTRAINT fk_datasetnodeinformation_version_id_datasetversionspecific FOREIGN KEY(version_id) REFERENCES datasetversionspecific (version_id)
);

CREATE INDEX ix_datasetnodeinformation_version_id ON datasetnodeinformation (version_id);

CREATE TABLE rawdocversionlink (
	id INTEGER NOT NULL,
	raw_id INTEGER NOT NULL,
	version_id VARCHAR NOT NULL,
	CONSTRAINT pk_rawdocversionlink PRIMARY KEY (id),
	CONSTRAINT uq_rawdocversionlink_raw_id_version_id UNIQUE (raw_id, version_id),
	CONSTRAINT fk_rawdocversionlink_raw_id_datasetrawdoc FOREIGN KEY(raw_id) REFERENCES datasetrawdoc (id),
	CONSTRAINT fk_rawdocversionlink_version_id_datasetversionspecific FOREIGN KEY(version_id) REFERENCES datasetversionspecific (version_id)
);

CREATE INDEX ix_rawdocversionlink_raw_id ON rawdocversionlink (raw_id);

CREATE INDEX ix_rawdocversionlink_version_id ON rawdocversionlink (version_id);
