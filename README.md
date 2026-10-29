# Octopus Energy & Tado Energy IQ Sync Tool

This repository contains a script to automatically sync your Octopus Energy
smart meter readings with Tado's Energy IQ feature. The workflow provided allows
you to set this up to run on a weekly basis using GitHub Actions, so your Tado
Energy IQ remains up-to-date without any manual effort.

**Note**: This tool is specifically oriented toward users with smart gas meters
from Octopus Energy.

## Features

- Automatically pulls your gas usage data from Octopus Energy.
- Syncs the data with Tado Energy IQ for better home energy management insights.
- Optionally syncs the current/historical Octopus gas unit tariff into Tado Energy IQ.
- Set up once, and it runs weekly via GitHub Actions.

## Setup Instructions

Follow these steps to configure the sync for your own Octopus Energy and Tado
accounts:

### 1. Fork This Repository

First, fork this repository to your own GitHub account. This will allow you to
customize the secrets specific to your accounts and run the workflow
independently.

### 2. Configure GitHub Secrets

In order to use the script, you'll need to provide credentials for both your
Tado account and Octopus Energy API. This is done through GitHub secrets.

1. Go to the **Settings** tab of your forked repository.
2. In the left-hand menu, select **Secrets and variables** > **Actions**.
3. Click **New repository secret** and add the following secrets:

| Secret Name              | Description                                                   |
|--------------------------|---------------------------------------------------------------|
| `TADO_EMAIL`             | The email address associated with your Tado account.          |
| `TADO_PASSWORD`          | The password for your Tado account.                           |
| `OCTOPUS_MPRN`           | Your gas MPRN (Meter Point Reference Number).                 |
| `OCTOPUS_GAS_SERIAL`     | The serial number of your gas meter.                          |
| `OCTOPUS_API_KEY`        | Your Octopus Energy API key. You can obtain this from the Octopus Energy developer portal (details below). |
| `OCTOPUS_ACCOUNT_NUMBER` | Your Octopus account number. Required only if tariff sync is enabled. |

### 3. Obtain Your Octopus Energy Details

To find your **API Key**, **Gas MPRN**, and **Gas Serial Number**, follow these
steps:

1. Log into your [Octopus Energy
account](https://octopus.energy/dashboard/new/accounts/personal-details/api-access).
2. Navigate to the "API Access" section of your account. Here, you'll find your
**API Key**.
3. Your **Gas MPRN** and **Gas Serial Number** can also be found in this
section.

These details are necessary to allow the script to pull your gas usage data from
Octopus Energy.

If you want to sync tariffs as well, you will also need your **Octopus account
number**. The script uses the account endpoint to discover the active gas
agreement and then fetches the matching unit-rate history.

### 4. Enable the Workflow

The repository is already set up with a GitHub Actions workflow that runs the
sync script once a week. The workflow is located at
`.github/workflows/schedule_sync.yml`. After you’ve added your secrets, the
workflow will automatically begin running on schedule.

You can manually trigger the workflow by navigating to the **Actions** tab in
your repository and selecting the sync workflow.

### 5. (Optional) Customize the Schedule

By default, the workflow runs weekly. If you want to change the schedule:

1. Open the `.github/workflows/schedule_sync.yml` file in your repository.
2. Modify the schedule trigger under `on: schedule:` following [the cron
syntax](https://crontab.guru/) for the desired frequency.

For example, to run daily at midnight:

```yaml
on:
  schedule:
    - cron: '0 0 * * *'
```

### 6. Monitor the Workflow

You can check the status of the sync runs in the **Actions** tab of your GitHub
repository. Here, you can see past runs, their logs, and any errors that might
have occurred.

### Usage

The GitHub Actions workflow automatically runs the following script:

```bash
python sync_octopus_tado.py \
  --tado-email "${{ secrets.TADO_EMAIL }}" \
  --tado-password "${{ secrets.TADO_PASSWORD }}" \
  --mprn "${{ secrets.OCTOPUS_MPRN }}" \
  --gas-serial-number "${{ secrets.OCTOPUS_GAS_SERIAL }}" \
  --octopus-api-key "${{ secrets.OCTOPUS_API_KEY }}"
```

The script will:

1. Fetch the most recent gas usage readings from your Octopus Energy account
using their API.
2. Sync these readings with Tado's Energy IQ to keep your gas consumption
insights up-to-date.
3. Optionally sync Octopus gas **unit tariff** changes to Tado when
   `--update-tariff` is passed and `OCTOPUS_ACCOUNT_NUMBER` is configured.

The supplied workflow uses `--meter-source calibrated-cumulative`. It validates
the Octopus cumulative register against recent daily usage and submits only
when Octopus has a reading newer than Tado's latest meter reading. Add
`--dry-run-meter` to calculate and report a proposal without submitting it.

### Optional Tariff Sync

Pass `--update-tariff` to enable tariff sync. When enabled, the script:

1. Reads the latest tariff history already stored in Tado.
2. Uses the Octopus account API to find the matching gas agreement.
3. Fetches missing tariff periods from Octopus product tariff endpoints.
4. Uploads only the missing tariff periods to Tado.

This keeps the workflow stateless and suitable for GitHub Actions.

Example:

```bash
python sync_octopus_tado.py \
  --tado-email "$TADO_EMAIL" \
  --tado-password "$TADO_PASSWORD" \
  --mprn "$OCTOPUS_MPRN" \
  --gas-serial-number "$OCTOPUS_GAS_SERIAL" \
  --octopus-api-key "$OCTOPUS_API_KEY" \
  --octopus-account-number "$OCTOPUS_ACCOUNT_NUMBER" \
  --update-tariff
```

> Note: the current implementation syncs the **gas unit rate** only. Octopus
> standing charges are not currently pushed into Tado.

### Troubleshooting

- **Tado login failures**: The log and job summary identify client initialization,
  browser authorization, or device token activation. OAuth requests without an
  explicit timeout use 10 seconds to connect and 30 seconds to read; existing
  explicit library timeouts are retained. The scheduled/manual job has a
  15-minute limit. Login must reach `COMPLETED` before sync starts. Transport
  errors are distinguished from confirmed HTTP 429 responses, without printing
  the original exception chain or device-code URLs. Token exchanges are not
  automatically retried. Token storage remains runner-local, so a fresh GitHub
  runner still performs device authorization.
- **Incorrect credentials**: If the script fails due to incorrect credentials,
  ensure that the email, password, MPRN, and serial number are accurate. Verify
that your Octopus API key is valid.
- **Workflow failures**: Detailed logs for each sync run can be found in the
  **Actions** tab of your repository. Use these logs to identify and
troubleshoot any issues.
- **Cumulative calibration failures**: The job log and summary show the number
  of matched days, median ratio, and each matched date's ratio and elapsed
  hours. The error distinguishes insufficient matches from inconsistent
  ratios. Calibration uses only consumption dates in the 14-day window ending
  at the delayed cutoff; older records remain available for checkpoint lookup.
  Individual ratios retain the 0.01 tolerance. An outlying day can be paired
  with an adjacent day only if their opposite discrepancies cancel exactly
  against the median factor. Each pair counts as one independent group, with
  at least three groups required. The shifted middle-day register cannot be
  used as a submission checkpoint. Unresolved discrepancies stop submission.
- **Tariff failures**: A failed tariff sync now fails the job. Unit-rate lookup
  errors identify the product and tariff codes, which helps locate a 404.
  Both legacy gas codes such as `G-ISDFEDV1023SR1R-J` and newer `G-1R-...`
  codes are supported. Agreements ending at or before the latest stored Tado
  tariff start are skipped before rate lookup; required history is retained.
  Meter sync runs first, so an earlier meter submission may already have
  succeeded; tariff failure does not roll it back. Inspect the log before
  deciding to rerun.
- **Tariff date validation**: UTC tariff timestamps are converted to UK calendar
  dates before exclusive Octopus end boundaries become inclusive Tado end
  dates. Each period is limited by both agreement and rate validity. Gaps are
  preserved; overlapping or ambiguous periods and intraday changes stop the
  sync before tariff submission. UK conversion uses the pinned
  `python-dateutil` dependency on Windows and Linux.
- **Tado tariff rejections**: An error response or missing creation ID stops
  submission immediately and fails the job, with the accepted count and failed
  start date in the summary. Unreadable tariff history prevents blind backfill.
  These safeguards do not repair existing tariff records or refill historical
  gaps behind the latest stored checkpoint; those require a separate audit
  and an approved repair plan.

### Contributions

Feel free to contribute to this project by opening issues or submitting pull
requests. Any improvements, bug fixes, or new feature suggestions are welcome!

### License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file
for details.
