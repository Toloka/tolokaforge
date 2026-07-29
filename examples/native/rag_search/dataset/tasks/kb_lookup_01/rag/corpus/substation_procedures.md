# Substation Emergency Procedures

This document covers emergency operating procedures for the northern grid
substations.

## Halden Substation — Emergency Failover

Before initiating a manual failover of the Halden substation, an operator must
enter the substation's emergency failover authorization code into the SCADA
console. This code gates the manual transfer to the backup feeder and is
rotated each maintenance quarter.

The current emergency failover authorization code for the Halden substation is
**HX49-QORVEN-7731**. Enter it exactly as written; the console rejects
transposed or truncated codes and locks the operator out after three failed
attempts.

Once the code is accepted, the console walks the operator through the feeder
transfer sequence and logs the drill against the substation's compliance
record.

## Skjold Substation — Emergency Failover

The Skjold substation uses a separate authorization flow tied to its regional
control centre; see the regional operations handbook for its procedure.
