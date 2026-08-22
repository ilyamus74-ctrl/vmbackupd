(function () {
    "use strict";

    window.VmbackupModel = {

        deriveModel(data, now) {
            return {
                status: data.status || {},

                discoveredVms: Array.isArray(data.inventory)
                    ? data.inventory
                    : [],

                registeredVms: Array.isArray(data.registeredVms)
                    ? data.registeredVms
                    : [],

                storage: Array.isArray(data.storage)
                    ? data.storage
                    : [],

                jobs: Array.isArray(data.jobs)
                    ? data.jobs
                    : [],

                runs: Array.isArray(data.runs)
                    ? data.runs
                    : [],

                runPage: data.runPage || {},

                recovery: Array.isArray(data.recovery)
                    ? data.recovery
                    : [],


                jobById: new Map(
                    (Array.isArray(data.jobs) ? data.jobs : [])
                        .map(job => [job.id, job])
                ),

                vmById: new Map(
                    (Array.isArray(data.inventory) ? data.inventory : [])
                        .map(vm => [vm.id || vm.uuid, vm])
                ),


                now,

                successfulToday:
                    (Array.isArray(data.runs) ? data.runs : [])
                        .filter(run => run.status === "SUCCESS")
                        .length,

                failedToday:
                    (Array.isArray(data.runs) ? data.runs : [])
                        .filter(run => run.status === "FAILED")
                        .length,

                active:
                    (Array.isArray(data.runs) ? data.runs : [])
                        .filter(run =>
                            !["SUCCESS", "FAILED"].includes(run.status)
                        )
                        .length,

                recoveryRequired:
                    Array.isArray(data.recovery)
                        ? data.recovery.length
                        : 0,


                generatedAt: now,
            };
        },

        validate(model) {
            return Boolean(
                model &&
                typeof model === "object"
            );
        },

    };

    console.log(
        "MODEL READY",
        window.VmbackupModel
    );

})();
