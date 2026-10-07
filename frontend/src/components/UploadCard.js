import { useEffect, useState } from 'react';
import {
  Box,
  Button,
  Card,
  GridList,
  GridListTile,
  Link,
  Paper,
  Typography,
} from '@material-ui/core';

import localStackLogo from '../assets/images/localStackLogo.png';

function StatusPanel(props) {
  const { status } = props;
  if (!status) {
    return (
      <Paper style={{ padding: '10px', marginTop: '10px' }}>
        <Typography variant="body2">Loading storage status…</Typography>
      </Paper>
    );
  }
  const bucketState = status.bucket_probe_error
    ? `unknown (${status.bucket_probe_error})`
    : status.bucket_exists
    ? 'exists'
    : 'missing';
  const bucketColor = status.bucket_probe_error
    ? 'orange'
    : status.bucket_exists
    ? 'limeGreen'
    : 'red';
  let uploadState = 'no upload yet';
  let uploadColor = 'grey';
  if (status.last_upload) {
    if (status.last_upload.ok) {
      uploadState = `ok: ${status.last_upload.key}`;
      uploadColor = 'limeGreen';
    } else {
      uploadState = `failed: ${status.last_upload.key} (${status.last_upload.error})`;
      uploadColor = 'red';
    }
  }
  return (
    <Paper
      style={{ padding: '10px', marginTop: '10px' }}
      data-testid="storage-status-panel"
    >
      <Typography variant="subtitle2">Storage status</Typography>
      <Typography variant="caption">
        <div>
          Client source: <b>{status.client_source}</b>{' '}
          {status.client_cached ? '(cached)' : '(not yet built)'}
        </div>
        <div>
          Bucket <code>{status.bucket}</code>:{' '}
          <b style={{ color: bucketColor }}>{bucketState}</b>
        </div>
        <div>
          Last upload:{' '}
          <b style={{ color: uploadColor }}>{uploadState}</b>
          {status.last_upload && status.last_upload.at ? (
            <span> at {status.last_upload.at}</span>
          ) : null}
        </div>
      </Typography>
    </Paper>
  );
}

function UploadCard(props) {
  const [isLoaded, setIsLoaded] = useState(false);
  const [data, setData] = useState([]);
  const [error, setError] = useState(null);
  const [status, setStatus] = useState(null);
  const { classes } = props;

  const fetchStatus = () => {
    fetch('api/v1/files/status')
      .then((res) => res.json())
      .then(
        (result) => setStatus(result),
        () => setStatus(null)
      );
  };

  const fetchBucketFiles = () => {
    fetch('api/v1/files')
      .then((res) => res.json())
      .then(
        (result) => {
          setIsLoaded(true);
          const bucket = result['Name'];
          const filenames = result['Contents']
            ? result['Contents']
                .map((file) => `${bucket}/${file['Key']}`)
                .reverse()
            : [];
          setData(filenames);
        },
        (error) => {
          setIsLoaded(true);
          setError(error);
        }
      );
  };

  useEffect(() => {
    fetchBucketFiles();
    fetchStatus();
  }, []);

  const submitFile = async (e) => {
    e.preventDefault();
    const file = e.target.files;
    try {
      if (!file) {
        throw new Error('Please select an image');
      }
      const formData = new FormData();
      formData.append('file', file[0]);
      await fetch('/api/v1/files/upload/', {
        method: 'POST',
        body: formData,
      });
      fetchBucketFiles();
      fetchStatus();
    } catch (error) {
      alert(error);
    }
  };

  return (
    <Card className={classes.card}>
      <h2>Store files in S3, locally</h2>
      <Box
        display="flex"
        flexDirection="row"
        alignItems="center"
        mt={-3}
        mb={-1}
      >
        <Box mr={1}>
          <h3>Powered by</h3>
        </Box>
        <img
          width="75px"
          height="100%"
          src={localStackLogo}
          alt="LocalStack Logo"
        />
      </Box>
      <p>Click below to select and upload an image from your computer.</p>
      <p>
        The image will be <b>stored in a local S3 bucket</b>, powered by{' '}
        <Link
          className={classes.link}
          target="_blank"
          rel="noopener"
          href="https://github.com/localstack/localstack"
        >
          <b>LocalStack</b>
        </Link>{' '}
        - a fully functional local AWS cloud stack, and displayed below.
      </p>
      <form>
        <Box
          display="flex"
          flexDirection="column"
          alignItems="center"
          justifyContent="center"
          mt={2}
        >
          <Button
            variant="contained"
            className={classes.contained}
            component="label"
            size="small"
          >
            Upload Image
            <input
              type="file"
              accept=".jpg,.jpeg,.png,.gif"
              onChange={(event) => {
                submitFile(event);
              }}
              hidden
            />
          </Button>
        </Box>
      </form>

      <StatusPanel status={status} />

      {data ? (
        <Box display="flex" flexDirection="row" justifyContent="center">
          <div className={classes.gridContainer}>
            <GridList cellHeight={100} cols={3}>
              {data.map((src) => (
                <GridListTile key={src} cols={1}>
                  <img src={src} alt="Uploaded to LocalStack" />
                </GridListTile>
              ))}
            </GridList>
          </div>
        </Box>
      ) : (
        ''
      )}
    </Card>
  );
}

export default UploadCard;
