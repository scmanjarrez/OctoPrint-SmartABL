/*
 * View model for OctoPrint-SmartABL
 *
 * Author: scmanjarrez
 * License: AGPLv3
 */
$(function() {
    function SmartABLViewModel(parameters) {
        var self = this;
        var PLUGIN_ID = 'SmartABL'

        $(function() {
        	var btnGroupSmartABL = `<div id='job_smartABL' class='btn-group smartabl' data-toggle='buttons-radio'>
                                        <button id='smartABL_restricted' type='button' class='btn smartabl-radio'><i class='fa fa-crosshairs'></i><span> ABL Restricted</span></button>
                                        <button id='smartABL_counter' class='btn btn-info smartabl-counter disabled'>?</button>
                                        <button id='smartABL_always' type='button' class='btn smartabl-radio'><i class='fa fa-crosshairs'></i><span> ABL Always</span></button>
                                    </div>`;
            var btn_last_job = $('#job_print').parent().children().last();
            btn_last_job.after(btnGroupSmartABL);

            var counter = $('#smartABL_counter')

            var restricted = $('#smartABL_restricted')
            restricted.click(function() {
                restricted.addClass('btn-success');
                always.removeClass('btn-info');
                $.ajax({
                    url: API_BASEURL + "plugin/" + PLUGIN_ID,
                    type: "POST",
                    dataType: "json",
                    data: JSON.stringify({
                        command: "abl_always",
                        value: false
                    }),
                    contentType: "application/json; charset=UTF-8"
                });
            });

            var always = $('#smartABL_always')
            always.click(function() {
                always.addClass('btn-info');
                restricted.removeClass('btn-success');
                $.ajax({
                    url: API_BASEURL + "plugin/" + PLUGIN_ID,
                    type: "POST",
                    dataType: "json",
                    data: JSON.stringify({
                        command: "abl_always",
                        value: true
                    }),
                    contentType: "application/json; charset=UTF-8"
                });
            });

            self.onDataUpdaterPluginMessage = function(plugin, data) {
                if (plugin != PLUGIN_ID) {
                    return;
                }
                if (data.abl_always !== undefined) {
                    if (data.abl_always) {
                        always.click();
                    } else {
                        restricted.click();
                    }
                } else if (data.abl_counter !== undefined) {
                    counter.text(data.abl_counter[0].toString().concat("/", data.abl_counter[1]))
                } else {
                    new PNotify({
						title: data.abl_notify[0],
						text: data.abl_notify[1],
						type: "error",
					});
                }
            }
        });
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: SmartABLViewModel
    });
});
